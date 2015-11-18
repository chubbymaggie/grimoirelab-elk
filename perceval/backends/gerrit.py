#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# GitHub Pull Requests for Elastic Search
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

'''Gerrit backend for Gerrit'''


from datetime import datetime
from dateutil import parser
import json
import logging
import os
import re
import requests
import subprocess
from perceval.utils import get_eta, remove_last_char_from_file

from perceval.backends.backend import Backend


class Gerrit(Backend):

    _name = "gerrit"

    @classmethod
    def add_params(cls, cmdline_parser):
        parser = cmdline_parser

        parser.add_argument("--user",
                            help="Gerrit ssh user")
        parser.add_argument("-u", "--url", required=True,
                            help="Gerrit url")
        parser.add_argument("--nreviews",  default=500, type=int,
                            help="Number of reviews per ssh query")
        parser.add_argument("--sortinghat_db",  required=True,
                            help="Sorting Hat database")
        parser.add_argument("--gerrit_grimoirelib_db",  required=True,
                            help="GrimoireLib gerrit database")
        parser.add_argument("--projects_grimoirelib_db",  required=True,
                            help="GrimoireLib projects database")

        Backend.add_params(cmdline_parser)


    def __init__(self, user, repository, nreviews,
                 use_cache = False, incremental = True):

        self.gerrit_user = user
        self.project = repository
        self.nreviews = nreviews
        self.reviews = []  # All reviews from gerrit
        self.projects = []  # All projects from gerrit
        self.cache = {}  # cache projects listing
        self.url = repository
        self.elastic = None  # used for dump and restore

        self.gerrit_cmd  = "ssh -p 29418 %s@%s" % (user, repository)
        self.gerrit_cmd += " gerrit "

        # self.max_reviews = 50000  # around 2 GB of RAM
        self.max_reviews = 1000 * 100

        super(Gerrit, self).__init__(use_cache, incremental)


    def _get_name(self):

        return Gerrit._name

    def get_id(self):
        ''' Return gerrit unique identifier '''

        return self._get_name() + "_" + self.url

    def get_url(self):

        return self.url

    def _restore_state(self):
        '''Restore JSON full data from storage (ES) '''

        # See last_date to start from last gerrit state

        pass  # It is done when getting reviews


    def _dump_state(self):
        ''' Dump JSON full data to storage (ES)'''

        # See _reviews_state_to_es

        pass



    def _clean_cache(self):

        filelist = [ f for f in os.listdir(self._get_storage_dir()) if
                    f.startswith("cache_issue_") ]
        for f in filelist:
            os.remove(os.path.join(self._get_storage_dir(), f))


    def _close_cache(self):

        pass  # not needed in gerrit


    def _reviews_to_cache(self, reviews):
        ''' Update pull requests cache files  '''

        for review in reviews:

            data_json = json.dumps(review)
            cache_file = os.path.join(self._get_storage_dir(),
                          "cache_review_%s.json" % (review['id']))

            with open(cache_file, "w") as cache:
                cache.write(data_json)


    def _get_reviews_from_cache(self):
        logging.info("Reading reviews from cache")
        # Just read all issues cache files
        filelist = [ f for f in os.listdir(self._get_storage_dir()) if
                    f.startswith("cache_review_") ]
        logging.debug("Total reviews in cache: %i" % (len(filelist)))
        for f in filelist:
            fname = os.path.join(self._get_storage_dir(), f)
            with open(fname,"r") as f:
                review = json.loads(f.read())
                self.reviews.append(review)
        logging.info("Cache read completed")

        return self


    def _get_version(self):
        gerrit_cmd_prj = self.gerrit_cmd + " version "

        raw_data = subprocess.check_output(gerrit_cmd_prj, shell = True)
        raw_data = str(raw_data, "UTF-8")

        # output: gerrit version 2.10-rc1-988-g333a9dd
        m = re.match("gerrit version (\d+)\.(\d+).*", raw_data)

        if not m:
            raise Exception("Invalid gerrit version %s" % raw_data)

        try:
            mayor = int(m.group(1))
            minor = int(m.group(2))
        except:
            raise Exception("Invalid gerrit version %s " %
                            (raw_data))

        return [mayor, minor]


    def _get_projects(self):
        """ Get all projects in gerrit """

        logging.debug("Getting list of gerrit projects")

        if self.use_cache:
            projects = self.cache['projects']
        else:
            gerrit_cmd_projects = self.gerrit_cmd + "ls-projects "
            projects_raw = subprocess.check_output(gerrit_cmd_projects, shell = True)


            projects_raw = str(projects_raw, 'UTF-8')
            projects = projects_raw.split("\n")
            projects.pop() # Remove last empty line

        logging.debug("Done")


        return projects

    def _get_field_unique_id(self):
        return "id"

    def _get_server_reviews(self, project = None):
        """ Get all reviews for all or for a project """

        if project:
            logging.info("Getting reviews for: %s" % (project))
        else:
            logging.info("Getting all reviews")

        last_update = self._get_last_date(project)

        logging.debug("Last update: %s" % (last_update))

        gerrit_version = self._get_version()
        last_item = None
        if gerrit_version[0] == 2 and gerrit_version[1] >= 9:
            last_item = 0

        gerrit_cmd_prj = self.gerrit_cmd + " query "
        if project:
            gerrit_cmd_prj +="project:"+project+" "
        gerrit_cmd_prj += "limit:" + str(self.nreviews)
        gerrit_cmd_prj += " --all-approvals --comments --format=JSON"
        logging.debug(gerrit_cmd_prj)

        number_results = self.nreviews

        reviews = []
        more_updates = True

        while (number_results == self.nreviews + 1 or # wikimedia gerrit returns limit+1
               number_results == self.nreviews) and \
               more_updates:

            cmd = gerrit_cmd_prj
            if last_item is not None:
                if gerrit_version[0] == 2 and gerrit_version[1] >= 9:
                    cmd += " --start=" + str(last_item)
                else:
                    cmd += " resume_sortkey:" + last_item

            raw_data = subprocess.check_output(cmd, shell = True)
            raw_data = str(raw_data, "UTF-8")
            tickets_raw = "[" + raw_data.replace("\n", ",") + "]"
            tickets_raw = tickets_raw.replace(",]", "]")

            tickets = json.loads(tickets_raw)

            for entry in tickets:

                if self.incremental and last_update:
                    if 'project' in entry.keys():
                        entry_lastUpdated = \
                            datetime.fromtimestamp(entry['lastUpdated'])
                        if entry_lastUpdated <= parser.parse(last_update):
                            if project:
                                logging.debug("No more updates for %s" % (project))
                            else:
                                logging.debug("No more updates for %s" % (self.url))
                            more_updates = False
                            break

                if 'project' in entry.keys():
                    reviews.append(entry)
                    if gerrit_version[0] == 2 and gerrit_version[1] >= 9:
                        last_item += 1
                    else:
                        last_item = entry['sortKey']
                elif 'rowCount' in entry.keys():
                    # logging.info("CONTINUE FROM: " + str(last_item))
                    number_results = entry['rowCount']


        self._reviews_to_cache(reviews)
        self._items_state_to_es(reviews)

        if self.incremental:
            logging.info("Total new reviews: %i" % len(reviews))
        else:
            logging.info("Total reviews: %i" % len(reviews))

        return reviews

    def _memory_usage(self):
        # return the memory usage in MB
        import psutil
        process = psutil.Process(os.getpid())
        mem = process.get_memory_info()[0] / float(2 ** 20)
        return mem

    def _get_last_date(self, project = None):

        _filter = None

        if project:
            _filter = {}
            _filter['name'] = 'project'
            _filter['value'] = project

        return self.elastic.get_last_date("reviews_state", "lastUpdated",
                                          _filter)


    def get_reviews(self):

        if self.use_cache:
            self._get_reviews_from_cache()
            return self.reviews

        # First we need all projects
        projects = self._get_projects()

        total = len(projects)
        current_repo = 1

        for project in projects:
            # if repository != "openstack/cinder": continue
            task_init = datetime.now()

            self.reviews += self._get_server_reviews(project)

            task_time = (datetime.now() - task_init).total_seconds()
            eta_time = task_time * (total-current_repo)
            eta_min = eta_time / 60.0

            logging.info("Completed %s %i/%i (ETA: %.2f min)\n" \
                             % (project, current_repo, total, eta_min))


            if len(self.reviews) >= self.max_reviews:
                # 5 GB RAM memory usage
                logging.error("Max reviews reached: %i " % (self.max_reviews))
                break

            logging.debug ("Total reviews in memory: %i" % (len(self.reviews)))
            logging.debug ("Total memory: %i MB" % (self._memory_usage()))

            current_repo += 1

        return self.reviews

    def _get_reviews_all(self):
        """ Get all reviews from the repository  """

        logging.error("Experimental feature for internal use.")

        return []

        task_init = datetime.now()
        self.reviews = self._get_server_reviews()
        task_time = (datetime.now() - task_init).total_seconds()

        logging.info("Completed in %.2f min\n" % (task_time))


        return self.reviews