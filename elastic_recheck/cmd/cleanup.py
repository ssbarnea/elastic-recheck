#!/usr/bin/env python

# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import argparse
import os

from elastic_recheck.cmd import graph
from elastic_recheck import elasticRecheck as er


# TODO(mriedem): We may want to include Incomplete and Won't Fix in
# this list since a bug could be reported against multiple projects but only
# fixed in one of them and marked Invalid in others.
FIXED_STATUSES = ('Invalid', 'Fix Committed', 'Fix Released')


class InvalidProjectstatus(Exception):
    """Indicates an error parsing a bug data's affected projects"""


def get_project_status(affected_bug_data):
    """Parses a (<project> - <status>) value

    :param affected_bug_data: String of the expected form
        "(<project> - <status>)".
    :raises: InvalidProjectStatus if the affected_bug_data string cannot be
        parsed
    :returns: Two-item tuple of:
        - project
        - status
    """
    # Note that the string can be "Unknown (Private Bug)" or "Unknown" so
    # handle parsing errors.
    project_status = affected_bug_data.split(' - ')
    if len(project_status) != 2:
        raise InvalidProjectstatus(affected_bug_data)
    # Trim leading ( and trailing ).
    return project_status[0][1:], project_status[1][:-1]


def main():
    parser = argparse.ArgumentParser(
        description='Remove old queries where the affected projects list the '
                    'bug status as one of: %s' % ', '.join(FIXED_STATUSES))
    parser.add_argument('--bug', metavar='<bug>',
                        help='Specific bug number/id to clean. Returns an '
                             'exit code of 1 if no query is found for the '
                             'bug.')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='Print out old queries that would be removed but '
                             'do not actually remove them.')
    parser.add_argument('-v', dest='verbose',
                        action='store_true', default=False,
                        help='Print verbose information during execution.')
    args = parser.parse_args()
    verbose = args.verbose
    dry_run = args.dry_run

    def info(message):
        if verbose:
            print(message)

    info('Loading queries')
    classifier = er.Classifier('queries')
    processed = []  # keep track of the bugs we've looked at
    cleaned = []  # keep track of the queries we've removed
    for query in classifier.queries:
        bug = query['bug']
        processed.append(bug)

        # If we're looking for a specific bug check to see if we found it.
        if args.bug and bug != args.bug:
            continue

        # Skip anything with suppress-graph: true since those are meant to be
        # kept around even if they don't have hits.
        if query.get('suppress-graph', False):
            info('Skipping query for bug %s since it has '
                 '"suppress-graph: true"' % bug)
            continue

        info('Getting data for bug: %s' % bug)
        bug_data = graph.get_launchpad_bug(bug)
        affects = bug_data.get('affects')
        # affects is a comma-separated list of (<project> - <status>), e.g.
        # "(neutron - Confirmed), (nova - Fix Released)".
        if affects:
            affects = affects.split(',')
            fixed_in_all_affected_projects = True
            for affected in affects:
                affected = affected.strip()
                try:
                    project, status = get_project_status(affected)
                    if status not in FIXED_STATUSES:
                        # TODO(mriedem): It could be useful to report queries
                        # that do not have hits but the bug is not marked as
                        # fixed.
                        info('Bug %s is not fixed for project %s' %
                             (bug, project))
                        fixed_in_all_affected_projects = False
                        break
                except InvalidProjectstatus:
                    print('Unable to parse project status "%s" for bug %s' %
                          (affected, bug))
                    fixed_in_all_affected_projects = False
                    break

            if fixed_in_all_affected_projects:
                # TODO(mriedem): It might be good to sanity check that a query
                # does not have hits if we are going to remove it even if the
                # bug is marked as fixed, e.g. bug 1745168. The bug may have
                # re-appeared, or still be a problem on stable branches, or the
                # query may be too broad.
                if dry_run:
                    info('[DRY-RUN] Remove query for bug: %s' % bug)
                else:
                    info('Removing query for bug: %s' % bug)
                    os.remove('queries/%s.yaml' % bug)
                cleaned.append(bug)
        else:
            print('Unable to determine affected projects for bug %s' % bug)

    # If a specific bug was provided did we find it?
    if args.bug and args.bug not in processed:
        print('Unable to find query for bug: %s' % args.bug)
        return 1

    # Print a summary of what we cleaned.
    prefix = '[DRY-RUN] ' if dry_run else ''
    # If we didn't remove anything, just print None.
    if not cleaned:
        cleaned.append('None')
    info('%sRemoved queries:\n%s' % (prefix, '\n'.join(sorted(cleaned))))


if __name__ == "__main__":
    main()
