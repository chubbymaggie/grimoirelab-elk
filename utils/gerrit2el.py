#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Gerrit reviews loader for Elastic Search
#
# Copyright (C) 2015 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#   Alvaro del Castillo San Felix <acs@bitergia.com>
#
# TODO: Just a playing script yet.
#    - Add params support so config options are not hard coded
#    - Analyze all repos if there is no projects db
#    - Check needed dbs exists

from datetime import datetime
import json
import logging
import MySQLdb
import os
import re
import subprocess
import time
import urllib2

# https://github.com/jgbarah/Grimoire-demo/blob/master/grimoire-ng-data.py#L338
class Database:
    """To work with a database (likely including several schemas).
    """

    def __init__ (self, user, passwd, host, port, scrdb, shdb, prjdb):
        self.user = user
        self.passwd = passwd
        self.host = host
        self.port = port
        self.scrdb = scrdb
        self.shdb = shdb
        self.prjdb = prjdb
        self.db, self.cursor = self._connect()

    def _connect(self):
        """Connect to the MySQL database.
        """

        try:
            db = MySQLdb.connect(user = self.user, passwd = self.passwd,
                                 host = self.host, port = self.port,
                                 db = self.shdb,
                                 use_unicode = True)
            return db, db.cursor()
        except:
            logging.error("Database connection error")
            raise

    def execute(self, query):
        """Execute an SQL query with the corresponding database.
        The query can be "templated" with {scm_db} and {sh_db}.
        """

        # sql = query.format(scm_db = self.scmdb,
        #                   sh_db = self.shdb,
        #                   prj_db = self.prjdb)

        results = int (self.cursor.execute(query))
        if results > 0:
            result1 = self.cursor.fetchall()
            return result1
        else:
            return []


def fix_review_dates(item):
    """ Convert dates so ES detect them """

    for date_field in ['timestamp','createdOn','lastUpdated']:
        if date_field in item.keys():
            date_ts = item[date_field]
            item[date_field] = time.strftime('%Y-%m-%dT%H:%M:%S',
                                              time.localtime(date_ts))
    if 'patchSets' in item.keys():
        for patch in item['patchSets']:
            pdate_ts = patch['createdOn']
            patch['createdOn'] = time.strftime('%Y-%m-%dT%H:%M:%S',
                                               time.localtime(pdate_ts))
            if 'approvals' in patch:
                for approval in patch['approvals']:
                    adate_ts = approval['grantedOn']
                    approval['grantedOn'] = time.strftime('%Y-%m-%dT%H:%M:%S',
                                                          time.localtime(adate_ts))
    if 'comments' in item.keys():
        for comment in item['comments']:
            cdate_ts = comment['timestamp']
            comment['timestamp'] = time.strftime('%Y-%m-%dT%H:%M:%S',
                                                 time.localtime(cdate_ts))

def get_repositories():
    """ Get all repositories in gerrit """

    cache_file = "gerrit-repositories_cache.json"
    cache_file = os.path.join(cache_dir, cache_file)

    if not os.path.isfile(cache_file):
        gerrit_cmd_projects = gerrit_cmd + "ls-projects "
        raw_data = subprocess.check_output(gerrit_cmd_projects, shell = True)
        with open(cache_file, 'w') as f:
            f.write(raw_data)
    else:
        with open(cache_file) as f:
            raw_data = f.read()

    projects = raw_data.split("\n")
    projects.pop() # Remove last empty line

    return projects

def get_repositories_from_projects():
    """ Get the repositories list from OpenStack official projects """

    repos_list = []

    gerrit_projects_db = "amartin_projects_openstack_sh"

    db = Database (user = "root", passwd = "",
                   host = "localhost", port = 3306,
                   scrdb = None, shdb = gerrit_projects_db, prjdb = None)

    sql = """
        SELECT DISTINCT(repository_name) 
        FROM project_repositories
        WHERE data_source='scr'
    """

    repos_list_raw = db.execute(sql)

    # Convert from review.openstack.org_openstack/rpm-packaging-tools to
    # openstack_rpm-packaging-tools
    for repo in repos_list_raw:
        repo_name = repo[0].replace("review.openstack.org_","")
        repos_list.append(repo_name)

    return repos_list


def get_uuid(email):
    """ Get in the most efficient way the uuid (people unique identifier)
        for an email """

    uuid = None
    try:
        uuid = email2uuid[email]
    except:
        emailNOuuid.add(email)
        pass

    return uuid


def get_org(uuid, action_date_str):
    """ Get in the most efficient way the organization for
        uuid in the date when an action was done  """

    org_found = "Unknown"
    action_date = datetime.strptime(action_date_str, "%Y-%m-%dT%H:%M:%S")

    try:
        orgs = uuid2orgs[uuid]
        # Take the org active in action_date
        for org in orgs:
            if org['start'] < action_date and org['end'] >= action_date:
                org_found = org['name']
                break
    except:
        # logging.info("Can't find org for " + email)
        pass

    return org_found

def get_org_by_email(email, action_date_str):
    """ Get in the most efficient way the organization for
        email in the date when an action was done  """

    org_found = get_org(get_uuid(email), action_date_str)

    if org_found == "Unknown":
        # Try to get the org from the email domain
        try:
            domain = email.split('@')[1]
            org_found = domain2org[domain]
        except:
            pass # domain not found

    return org_found


def get_isbot(uuid):
    """ Get if an uuid is a bot  """

    bot = 0 # Default uuid is not a bot
    try:
        bot = uuid2bot[uuid]
    except:
        # logging.info("Can't find org for " + email)
        pass

    return bot


def create_events_map():
    elasticsearch_type = "reviews_events"
    url = elasticsearch_url + "/"+elasticsearch_index
    url_type = url + "/" + elasticsearch_type

    reviews_events_map = """
    {
        "properties": {
           "approval_email": {
              "type": "string",
              "index":"not_analyzed"
           },
           "approval_grantedOn": {
              "type": "date",
              "format": "dateOptionalTime"
           },
           "approval_organization": {
              "type": "string",
              "index":"not_analyzed"
           },
           "approval_type": {
              "type": "string",
              "index":"not_analyzed"
           },
           "approval_username": {
              "type": "string"
           },
           "approval_value": {
              "type": "long"
           },
           "patchSet_createdOn": {
              "type": "date",
              "format": "dateOptionalTime"
           },
           "patchSet_email": {
              "type": "string",
              "index":"not_analyzed"
           },
           "patchSet_id": {
              "type": "string"
           },
           "patchSet_organization": {
              "type": "string",
              "index":"not_analyzed"
           },
           "review_branch": {
              "type": "string",
              "index":"not_analyzed"
           },
           "review_createdOn": {
              "type": "date",
              "format": "dateOptionalTime"
           },
           "review_email": {
              "type": "string",
              "index":"not_analyzed"
           },
           "review_id": {
              "type": "string"
           },
           "review_organization": {
              "type": "string",
              "index":"not_analyzed"
           },
           "review_project": {
              "type": "string",
              "index":"not_analyzed"
           },
           "review_status": {
              "type": "string",
              "index":"not_analyzed"
           }
        }
    }
    """

    # Create the index if it not exists
    request = urllib2.Request(url)
    request.get_method = lambda: 'PUT'
    try:
        opener.open(request)
    except:
        # The index already exists
        pass


    # Create mappings
    url_map = url_type+"/_mapping"
    request = urllib2.Request(url_map, data=reviews_events_map)
    request.get_method = lambda: 'PUT'
    opener.open(request)


def fetch_events(review):
    """ Fetch in ES patches and comments (events) as documents """

    bulk_json = ""  # Bulk JSON to be feeded in ES

    # Review fields included in all events
    bulk_json_review  = '"review_id":"%s",' % review['id']
    bulk_json_review += '"review_createdOn":"%s",' % review['createdOn']
    if 'owner' in review and 'email' in review['owner']:
        remail = review['owner']['email']
        ruuid = get_uuid(remail)
        bulk_json_review += '"review_email":"%s",' % remail
        bulk_json_review += '"review_uuid":"%s",' % ruuid
        bulk_json_review += '"review_organization":"%s",' % \
            get_org_by_email(remail, review['createdOn'])
        bulk_json_review += '"review_bot":"%s",' % get_isbot(ruuid)
    else:
        bulk_json_review += '"review_email":null,'
        bulk_json_review += '"review_organization":null,'
        bulk_json_review += '"review_bot":null,'
        bulk_json_review += '"review_uuid":null,'
    bulk_json_review += '"review_status":"%s",' % review['status']
    bulk_json_review += '"review_project":"%s",' % review['project']
    bulk_json_review += '"review_branch":"%s"' % review['branch']
    # bulk_json_review += '"review_subject":"%s"' % review['subject']
    # bulk_json_review += '"review_topic":"%s"' % review['topic']

    # To be used as review['createdOn'] which is wrong in OpenStack/Wikimedia
    firstPatchCreatedOn = review['patchSets'][0]['createdOn']

    for patch in review['patchSets']:
        # Patch fields included in all patch events
        bulk_json_patch  = '"patchSet_id":"%s",' % patch['number']
        bulk_json_patch += '"patchSet_createdOn":"%s",' % patch['createdOn']
        if 'author' in patch and 'email' in patch['author']:
            pemail = patch['author']['email']
            puuid = get_uuid(pemail)
            bulk_json_patch += '"patchSet_email":"%s",' % pemail
            bulk_json_patch += '"patchSet_uuid":"%s",' % puuid
            bulk_json_patch += '"patchSet_organization":"%s",' % \
                get_org_by_email(pemail, patch['createdOn'])
            bulk_json_patch += '"patchSet_bot":"%s"' % get_isbot(puuid)
        else:
            bulk_json_patch += '"patchSet_email":null,'
            bulk_json_patch += '"patchSet_uuid":null,'
            bulk_json_patch += '"patchSet_organization":null,'
            bulk_json_patch += '"patchSet_bot":null'

        app_count = 0  # Approval counter for unique id
        if 'approvals' not in patch:
            bulk_json_ap  = '"approval_type":null,'
            bulk_json_ap += '"approval_value":null,'
            bulk_json_ap += '"approval_email":null,'
            bulk_json_ap += '"approval_uuid":null,'
            bulk_json_ap += '"approval_organization":null,'
            bulk_json_ap += '"approval_bot":null'

            bulk_json_event = '{%s,%s,%s}' % (bulk_json_review,
                                              bulk_json_patch, bulk_json_ap)

            event_id = "%s_%s_%s" % (review['id'], patch['number'], app_count)
            bulk_json += '{"index" : {"_id" : "%s" } }\n' % (event_id)  # Bulk operation
            bulk_json += bulk_json_event +"\n"  # Bulk document

        else:
            for app in patch['approvals']:
                bulk_json_ap  = '"approval_type":"%s",' % app['type']
                bulk_json_ap += '"approval_value":%i,' % int(app['value'])
                bulk_json_ap += '"approval_grantedOn":"%s",' % app['grantedOn']
                if 'email' in app['by']:
                    aemail = app['by']['email']
                    auuid = get_uuid(aemail)
                    bulk_json_ap += '"approval_email":"%s",' % aemail
                    bulk_json_ap += '"approval_uuid":"%s",' % auuid
                    bulk_json_ap += '"approval_organization":"%s",' % \
                        get_org_by_email(aemail, app['grantedOn'])
                    bulk_json_ap += '"approval_bot":"%s",' % get_isbot(auuid)
                else:
                    bulk_json_ap += '"approval_email":null,'
                    bulk_json_ap += '"approval_uuid":null,'
                    bulk_json_ap += '"approval_organization":null,'
                    bulk_json_ap += '"approval_bot":null,'
                if 'username' in app['by']:
                    bulk_json_ap += '"approval_username":"%s",' % app['by']['username']
                else:
                    bulk_json_ap += '"approval_username":null,'

                # Time to add the time diffs
                app_time = \
                    datetime.strptime(app['grantedOn'], "%Y-%m-%dT%H:%M:%S")
                patch_time = \
                    datetime.strptime(patch['createdOn'], "%Y-%m-%dT%H:%M:%S")
                # review_time = \
                #    datetime.strptime(review['createdOn'], "%Y-%m-%dT%H:%M:%S")
                review_time = \
                    datetime.strptime(firstPatchCreatedOn, "%Y-%m-%dT%H:%M:%S")

                seconds_day = float(60*60*24)
                approval_time = \
                    (app_time-review_time).total_seconds() / seconds_day
                approval_patch_time = \
                    (app_time-patch_time).total_seconds() / seconds_day
                patch_time = \
                    (patch_time-review_time).total_seconds() / seconds_day
                bulk_json_ap += '"approval_time_days":%.2f,' % approval_time
                bulk_json_ap += '"approval_patch_time_days":%.2f,' % \
                    approval_patch_time
                bulk_json_ap += '"patch_time_days":%.2f' % patch_time

                bulk_json_event = '{%s,%s,%s}' % (bulk_json_review,
                                                  bulk_json_patch, bulk_json_ap)

                event_id = "%s_%s_%s" % (review['id'], patch['number'], app_count)
                bulk_json += '{"index" : {"_id" : "%s" } }\n' % (event_id)  # Bulk operation
                bulk_json += bulk_json_event +"\n"  # Bulk document

                app_count += 1

    url = elasticsearch_url+'/gerrit/reviews_events/_bulk'
    url = elasticsearch_url+'/gerrit_openstack/reviews_events/_bulk'
    request = urllib2.Request(url, data=bulk_json)
    request.get_method = lambda: 'POST'

    try:
        opener.open(request)
    except UnicodeEncodeError:
        logging.error("Events for review lost because Unicode error")
        print bulk_json
        request = urllib2.Request(url,
                                  data = bulk_json.encode('ascii', 'ignore'))
        request.get_method = lambda: 'POST'
        opener.open(request)


def getGerritVersion():
    gerrit_cmd_prj = gerrit_cmd + " version "

    raw_data = subprocess.check_output(gerrit_cmd_prj, shell = True)

    # output: gerrit version 2.10-rc1-988-g333a9dd
    m = re.match("gerrit version (\d+)\.(\d+).*", raw_data)

    if not m:
        raise Exception("Invalid gerrit version %s" % raw_data)

    try:
        mayor = int(m.group(1))
        minor = int(m.group(2))
    except Exception, e:
        raise Exception("Invalid gerrit version %s. Error: %s" %
                        (raw_data, str(e)))

    return [mayor, minor]

def get_project_reviews(project):
    """ Get all reviews for a project """

    gerrit_version = getGerritVersion()
    last_item = None
    if gerrit_version[0] == 2 and gerrit_version[1] >= 9:
        last_item = 0

    gerrit_cmd_prj = gerrit_cmd + " query project:"+project+" "
    gerrit_cmd_prj += "limit:" + str(max_items)
    gerrit_cmd_prj += " --all-approvals --comments --format=JSON"

    number_results = max_items

    reviews = []

    while (number_results == max_items or
           number_results == max_items + 1):  # wikimedia gerrit returns limit+1

        cmd = gerrit_cmd_prj
        if last_item is not None:
            if gerrit_version[0] == 2 and gerrit_version[1] >= 9:
                cmd += " --start=" + str(last_item)
            else:
                cmd += " resume_sortkey:" + last_item

        raw_data = subprocess.check_output(cmd, shell = True)
        tickets_raw = "[" + raw_data.replace("\n", ",") + "]"
        tickets_raw = tickets_raw.replace(",]", "]")

        tickets = json.loads(tickets_raw)

        for entry in tickets:
            if 'project' in entry.keys():
                reviews.append(entry)
                if gerrit_version[0] == 2 and gerrit_version[1] >= 9:
                    last_item += 1
                else:
                    last_item = entry['sortKey']
            elif 'rowCount' in entry.keys():
                # logging.info("CONTINUE FROM: " + str(last_item))
                number_results = entry['rowCount']

    logging.info("Total reviews: %i" % len(reviews))

    return reviews

def project_reviews_to_es(project):

    cache_file = "gerrit-"+project.replace("/","_")+"_cache.json"
    cache_file = os.path.join(cache_dir, cache_file)

    if not os.path.isfile(cache_file):
        # If data is not in cache, gather it
        reviews = get_project_reviews(project)
        raw_data = json.dumps(reviews)
        with open(cache_file, 'w') as f:
            f.write(raw_data)

    else:
        with open(cache_file) as f:
            raw_data = f.read()

    # Parse JSON document
    gerrit_json = json.loads(raw_data)

    elasticsearch_type = "reviews"

    for item in gerrit_json:
        fix_review_dates(item)

        data_json = json.dumps(item)
        url = elasticsearch_url + "/"+elasticsearch_index
        url += "/"+elasticsearch_type
        url += "/"+str(item["id"])
        request = urllib2.Request(url, data=data_json)
        request.get_method = lambda: 'PUT'
        opener.open(request)

        fetch_events(item)


def sortinghat_to_es():
    """ Load all identities data in SH in memory """

    logging.info("Loading Sorting Hat identities")

    sortinghat_db = "acs_sortinghat_mediawiki_5879"
    sortinghat_db = "amartin_sortinghat_openstack_sh"
    gerrit_grimoirelib_db = "amartin_bicho_gerrit_openstack_sh"
    db = Database (user = "root", passwd = "",
                   host = "localhost", port = 3306,
                   scrdb = None, shdb = sortinghat_db, prjdb = None)

    # Create the domain to orgs mapping
    sql = """
        SELECT domain, name
        FROM domains_organizations do
        JOIN organizations o ON o.id = do.organization_id;
    """
    domain2org_raw = db.execute(sql)
    for item in domain2org_raw:
        domain = item[0]
        org = item[1]
        domain2org[domain] = org

    # Create the uuids to orgs dict
    sql = """
        SELECT uuid, name, start, end
        FROM enrollments e
        JOIN organizations o ON e.organization_id = o.id
    """

    uuid2orgs_raw = db.execute(sql)

    for enrollment in uuid2orgs_raw:
        uuid = enrollment[0]
        org_name = enrollment[1]
        start = enrollment[2]
        end = enrollment[3]
        if uuid not in uuid2orgs: uuid2orgs[uuid] = []
        uuid2orgs[uuid].append({"name":org_name,
                                "start":start,
                                "end":end})

    # First using the email in profile table from sorting hat
    sql = """
        SELECT p.uuid, email, is_bot
        FROM profiles p
        WHERE email is not NULL
        """
    profiles = db.execute(sql)

    for profile in profiles:
        uuid = profile[0]
        email = profile[1]
        is_bot = profile[2]

        email2uuid[email] = uuid
        uuid2bot[uuid] = is_bot

    # Now using directly the grimoirelib gerrit identities
    sql = """
        SELECT uuid, email
        FROM %s.people p
        JOIN %s.people_uidentities pup ON p.id = pup.people_id
        """ % (gerrit_grimoirelib_db, gerrit_grimoirelib_db)

    profiles = db.execute(sql)

    for profile in profiles:
        uuid = profile[0]
        email = profile[1]

        email2uuid[email] = uuid


if __name__ == '__main__':

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    cache_dir = "cache"
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)

    # Reviews will be added to ES using its API REST
    # elasticsearch_url = "http://sega.bitergia.net:9200"
    elasticsearch_url = "http://localhost:9200"
    elasticsearch_index = "gerrit"
    elasticsearch_index = "gerrit_openstack"

    # Get reviews JSON data from gerrit using SSH
    max_items = 500
    gerrit_cmd  = "ssh -p 29418 acs@gerrit.wikimedia.org "
    gerrit_cmd  = "ssh -p 29418 acs@review.openstack.org "
    gerrit_cmd += "gerrit "
    # Feed the reviews items in EL
    opener = urllib2.build_opener(urllib2.HTTPHandler)

    # Add profiles to sortinghat
    email2uuid = {}
    emailNOuuid = set()
    uuid2orgs = {}
    uuid2bot = {}
    domain2org = {}
    sortinghat_to_es()

    # First we need all projects
    repositories = get_repositories()
    repos_projects = get_repositories_from_projects()

    total = len(repositories)
    current_repo = 1

    # Create the mapping for storing the events
    create_events_map()

    for repository in repositories:
        if repository not in repos_projects:
            logging.info("Not a project repository: " + repository)
            current_repo += 1
            continue
        # if repository != "openstack/cinder": continue
        logging.info("Processing repository:" + repository + " " +
                     str(current_repo) + "/" + str(total))
        project_reviews_to_es(repository)
        current_repo += 1

    logging.info("Emails without uuid: %i" % len(emailNOuuid))