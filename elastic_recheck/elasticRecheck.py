# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import dateutil.parser as dp
import gerritlib.gerrit
import pyelasticsearch
import sqlalchemy
from sqlalchemy import orm
from subunit2sql.db import api as db_api

import datetime
import logging
import re
import time

import elastic_recheck.config as er_conf
import elastic_recheck.loader as loader
import elastic_recheck.query_builder as qb
from elastic_recheck import results


def required_files(job):
    files = []
    if re.match("(tempest|grenade)-dsvm", job):
        files.extend([
            'logs/screen-n-api.txt',
            'logs/screen-n-cpu.txt',
            'logs/screen-n-sch.txt',
            'logs/screen-g-api.txt',
            'logs/screen-c-api.txt',
            'logs/screen-c-vol.txt',
            'logs/syslog.txt'])
        # we could probably add more neutron files
        # but currently only q-svc is used in queries
        if re.match("neutron", job):
            files.extend([
                'logs/screen-q-svc.txt',
                ])
        else:
            files.extend([
                'logs/screen-n-net.txt',
                ])
    # make sure that grenade logs exist
    if re.match("grenade", job):
        files.extend(['logs/grenade.sh.txt'])

    return files


def format_timedelta(td):
    """Format a timedelta value on seconds boundary."""
    return "%d:%2.2d" % (td.seconds / 60, td.seconds % 60)


class ConsoleNotReady(Exception):
    pass


class FilesNotReady(Exception):
    pass


class ResultTimedOut(Exception):
    pass


class FailJob(object):
    """A single failed job.

    A job is a zuul job.
    """
    bugs = []
    build_short_uuid = None
    url = None
    name = None

    def __init__(self, name, url):
        self.name = name
        self.url = url
        # The last set of characters of the URL are the first 7 digits
        # of the build_uuid.
        self.build_short_uuid = list(filter(None, url.split('/')))[-1]

    def __repr__(self):
        return self.name


class FailEvent(object):
    """A FailEvent consists of one or more FailJobs.

    An event is a gerrit event.
    """
    change = None
    rev = None
    project = None
    url = None
    build_short_uuids = []
    comment = None
    failed_jobs = []

    def __init__(self, event, failed_jobs, config=None):
        self.change = int(event['change']['number'])
        self.rev = int(event['patchSet']['number'])
        self.project = event['change']['project']
        self.url = event['change']['url']
        self.comment = event["comment"]
        self.created_on = event["eventCreatedOn"]
        # TODO(jogo): make FailEvent generate the jobs
        self.failed_jobs = failed_jobs
        self.config = config or er_conf.Config()

    def is_included_job(self):
        return re.search(self.config.jobs_re, self.comment)

    def name(self):
        return "%d,%d" % (self.change, self.rev)

    def bug_urls(self, bugs=None):
        if bugs is None:
            bugs = self.get_all_bugs()
        if not bugs:
            return None
        urls = ['https://bugs.launchpad.net/bugs/%s' % x for
                x in bugs]
        return urls

    def bug_list(self):
        """A pretty printed bug list."""
        return "- " + "\n- ".join(self.bug_urls_map())

    def bug_urls_map(self):
        """Produce sorted list of which jobs failed due to which bugs."""
        if not self.get_all_bugs():
            return None
        bug_map = {}
        for job in self.failed_jobs:
            if len(job.bugs) is 0:
                bug_map[job.name] = None
            else:
                bug_map[job.name] = ' '.join(self.bug_urls(job.bugs))
        bug_list = []
        for job in bug_map:
            if bug_map[job] is None:
                bug_list.append("%s: unrecognized error" % job)
            else:
                bug_list.append("%s: %s" % (job, bug_map[job]))
        return sorted(bug_list)

    def is_fully_classified(self):
        if self.get_all_bugs() is None:
            return True
        for job in self.failed_jobs:
            if len(job.bugs) is 0:
                return False
        return True

    def queue(self):
        # Assume one queue per gerrit event
        if len(self.failed_jobs) == 0:
            return None
        return self.failed_jobs[0].url.split('/')[6]

    def build_short_uuids(self):
        return [job.build_short_uuid for job in self.failed_jobs]

    def failed_job_names(self):
        return [job.name for job in self.failed_jobs]

    def get_all_bugs(self):
        bugs = set([])
        for job in self.failed_jobs:
            bugs |= set(job.bugs)
        if len(bugs) is 0:
            return None
        return list(bugs)

    def __repr__(self):
        return ("<FailEvent change:%s, rev:%s, project:%s,"
                "url:%s, comment:%s>" %
                (self.change, self.rev, self.project, self.url, self.comment))


class Stream(object):
    """Gerrit Stream.

    Monitors gerrit stream looking for tempest-devstack failures.
    """

    log = logging.getLogger("recheckwatchbot")

    def __init__(self, user, host, key, config=None, thread=True):
        self.config = config or er_conf.Config()
        port = 29418
        self.gerrit = gerritlib.gerrit.Gerrit(host, user, port, key)
        self.es = results.SearchEngine(self.config.es_url)
        if thread:
            self.gerrit.startWatching()

    @staticmethod
    def parse_jenkins_failure(event, ci_username=er_conf.CI_USERNAME):
        """Is this comment a jenkins failure comment."""
        if event.get('type', '') != 'comment-added':
            return False

        username = event['author'].get('username', '')
        if (username not in [ci_username, 'zuul']):
            return False

        if not ("Build failed" in
                event['comment']):
            return False

        failed_tests = []
        for line in event['comment'].split("\n"):
            # this is needed to know if we care about categorizing
            # these items. It's orthoginal to non voting ES searching.
            if " (non-voting)" in line:
                continue
            m = re.search("- ([\w-]+)\s*(http://\S+)\s*:\s*FAILURE", line)
            if m:
                failed_tests.append(FailJob(m.group(1), m.group(2)))
        return failed_tests

    def _job_console_uploaded(self, change, patch, name, build_short_uuid):
        query = qb.result_ready(change, patch, name, build_short_uuid)
        r = self.es.search(query, size='10', recent=True)
        if len(r) == 0:
            msg = ("Console logs not ready for %s %s,%s,%s" %
                   (name, change, patch, build_short_uuid))
            raise ConsoleNotReady(msg)
        else:
            self.log.debug("Console ready for %s %s,%s,%s" %
                           (name, change, patch, build_short_uuid))

    def _has_required_files(self, change, patch, name, build_short_uuid):
        query = qb.files_ready(change, patch, name, build_short_uuid)
        r = self.es.search(query, size='80', recent=True)
        files = [x['term'] for x in r.terms]
        # TODO(dmsimard): Reliably differentiate zuul v2 and v3 jobs
        required = required_files(name)
        missing_files = [x for x in required if x not in files]
        if (len(missing_files) != 0 or
           ('console.html' not in files and 'job-output.txt' not in files)):
            msg = ("%s missing for %s %s,%s,%s" % (
                missing_files, name, change, patch, build_short_uuid))
            raise FilesNotReady(msg)

    def _does_es_have_data(self, event):
        """Wait till ElasticSearch is ready, but return False if timeout."""
        # We wait 20 minutes wall time since receiving the event until we
        # treat the logs as missing
        timeout = 1200
        # Wait 40 seconds between queries.
        sleep_time = 40
        timed_out = False
        # This checks that we've got the console log uploaded, need to retry
        # in case ES goes bonkers on cold data, which it does some times.
        # We check at least once so that we can return success if data is
        # there. But then only check again until we reach a timeout since
        # the event was received.
        while True:
            try:
                for job in event.failed_jobs:
                    # TODO(jogo): if there are three failed jobs and only the
                    # last one isn't ready we don't need to keep rechecking
                    # the first two
                    self._job_console_uploaded(
                        event.change, event.rev, job.name,
                        job.build_short_uuid)
                    self._has_required_files(
                        event.change, event.rev, job.name,
                        job.build_short_uuid)
                break

            except ConsoleNotReady as e:
                self.log.debug(e)
            except FilesNotReady as e:
                self.log.info(e)
            except pyelasticsearch.exceptions.InvalidJsonResponseError:
                # If ElasticSearch returns an error code, sleep and retry
                # TODO(jogo): if this works pull out search into a helper
                # function that  does this.
                self.log.exception(
                    "Elastic Search not responding")
            # If we fall through then we had a failure of some sort.
            # Wait until timeout is exceeded.
            now = time.time()
            if now > event.created_on + timeout:
                # We've waited too long for this event, move on.
                timed_out = True
                break
            time.sleep(sleep_time)
        if timed_out:
            elapsed = now - event.created_on
            msg = ("Required files not ready after %ss for %s %d,%d,%s" %
                   (elapsed, job.name, event.change, event.rev,
                       job.build_short_uuid))
            raise ResultTimedOut(msg)

        self.log.debug(
            "Found hits for change_number: %d, patch_number: %d"
            % (event.change, event.rev))
        self.log.info(
            "All files present for change_number: %d, patch_number: %d"
            % (event.change, event.rev))
        return True

    def get_failed_tempest(self):
        self.log.debug("entering get_failed_tempest")
        while True:
            event = self.gerrit.getEvent()

            failed_jobs = Stream.parse_jenkins_failure(
                event, ci_username=self.config.ci_username)
            if not failed_jobs:
                # nothing to see here, lets try the next event
                continue

            fevent = FailEvent(event, failed_jobs, self.config)

            # bail if the failure is from a project
            # that hasn't run any of the included jobs
            if not fevent.is_included_job():
                continue

            self.log.info("Looking for failures in %d,%d on %s" %
                          (fevent.change, fevent.rev,
                           ", ".join(fevent.failed_job_names())))
            if self._does_es_have_data(fevent):
                return fevent

    def leave_comment(self, event, msgs, debug=False):
        parts = []
        if event.get_all_bugs():
            parts.append(msgs['found_bug'] % {'bugs': event.bug_list()})
            if event.is_fully_classified():
                parts.append(msgs['recheck_instructions'])
            else:
                parts.append(msgs['unrecognized'])
            parts.append(msgs['footer'])
        else:
            parts.append(msgs['no_bugs_found'])
        msg = '\n'.join(parts)
        self.log.debug("Compiled comment for commit %s:\n%s" %
                       (event.name(), msg))
        if not debug:
            self.gerrit.review(event.project, event.name(), msg)


def check_failed_test_ids_for_job(build_uuid, test_ids, session):
    failing_test_ids = db_api.get_failing_test_ids_from_runs_by_key_value(
        'build_short_uuid', build_uuid, session)
    for test_id in test_ids:
        if test_id in failing_test_ids:
            return True
    else:
        return False


class Classifier(object):
    """Classify failed tempest-devstack jobs based.

    Given a change and revision, query logstash with a list of known queries
    that are mapped to specific bugs.
    """
    log = logging.getLogger("recheckwatchbot")

    queries = None

    def __init__(self, queries_dir, config=None):
        self.config = config or er_conf.Config()
        self.es = results.SearchEngine(self.config.es_url)
        self.queries_dir = queries_dir
        self.queries = loader.load(self.queries_dir)

    def hits_by_query(self, query, queue=None, facet=None, size=100, days=0):
        if queue:
            es_query = qb.single_queue(query, queue, facet=facet)
        else:
            es_query = qb.generic(query, facet=facet)
        return self.es.search(es_query, size=size, days=days)

    def most_recent(self):
        """Return the datetime of the most recently indexed event."""
        query = qb.most_recent_event()
        results = self.es.search(query, size='1')
        if len(results) > 0:
            last = dp.parse(results[0].timestamp)
            return last
        return datetime.datetime.utcfromtimestamp(0)

    def classify(self, change_number, patch_number,
                 build_short_uuid, recent=False):
        """Returns either empty list or list with matched bugs."""
        self.log.debug("Entering classify")
        # Reload each time
        self.queries = loader.load(self.queries_dir)
        bug_matches = []
        engine = sqlalchemy.create_engine(self.config.db_uri)
        Session = orm.sessionmaker(bind=engine)
        session = Session()
        for x in self.queries:
            if x.get('suppress-notification'):
                continue
            self.log.debug(
                "Looking for bug: https://bugs.launchpad.net/bugs/%s"
                % x['bug'])
            query = qb.single_patch(x['query'], change_number, patch_number,
                                    build_short_uuid)
            results = self.es.search(query, size='10', recent=recent)
            if len(results) > 0:
                if x.get('test_ids', None):
                    test_ids = x['test_ids']
                    self.log.debug(
                        "For bug %s checking subunit2sql for failures on "
                        "test_ids: %s" % (x['bug'], test_ids))
                    if check_failed_test_ids_for_job(build_short_uuid,
                                                     test_ids, session):
                        bug_matches.append(x['bug'])
                else:
                    bug_matches.append(x['bug'])

        return bug_matches
