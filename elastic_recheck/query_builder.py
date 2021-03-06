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

"""Query builder for pyelasticsearch

A set of utility methods to build the kinds of queries that are needed
by elastic recheck to talk with elastic search.
"""

import json
from six.moves.urllib.parse import quote as urlquote


def generic(raw_query, facet=None):
    """Base query builder

    Takes a raw_query string for elastic search. This is typically the same
    content that you've typed into logstash to get to a unique result.

    Optionally supports a facet, which is required for certain operations,
    like ensuring that all the expected log files for a job have been
    uploaded.
    """

    # they pyelasticsearch inputs are incredibly structured dictionaries
    # so be it
    query = {
        "sort": {
            "@timestamp": {"order": "desc"}
            },
        "query": {
            "query_string": {
                "query": raw_query
                }
            }
        }
    # if we have a facet, the query gets expanded
    if facet:
        data = dict(field=facet, size=200)
        # yes, elasticsearch is odd, and the way to do multiple facets
        # is to specify the plural key value
        if type(facet) == list:
            data = dict(fields=facet, size=200)

        query['facets'] = {
            "tag": {
                "terms": data
                }
            }

    return query


def single_queue(query, queue, facet=None):
    """A query for a single queue."""
    return generic('%s '
                   'AND build_queue:"%s" ' %
                   (query, queue), facet=facet)


def result_ready(change, patchset, name, short_uuid):
    """A query to determine if we have a failure for a particular patch.

    This is looking for a particular FAILURE line in the console log, which
    lets us know that we've got results waiting that we need to process.
    """
    # TODO(dmsimard): Revisit this query once Zuul v2 is no longer supported
    # Let's value legibility over pep8 line width here...
    query = (
        '((filename:"job-output.txt" AND message:"POST-RUN END" AND message:"project-config/playbooks/base/post-ssh.yaml")'  # flake8: noqa
        ' OR '
        '(filename:"console.html" AND (message:"[Zuul] Job complete" OR message:"[SCP] Copying console log" OR message:"Grabbing consoleLog")))'  # flake8: noqa
        ' AND build_status:"FAILURE"'
        ' AND build_change:"{change}"'
        ' AND build_patchset:"{patchset}"'
        ' AND build_name:"{name}"'
        ' AND build_short_uuid:"{short_uuid}"'
    )
    return generic(query.format(
        change=change,
        patchset=patchset,
        name=name,
        short_uuid=short_uuid
    ))


def files_ready(review, patch, name, build_short_uuid):
    """A facetted query to ensure all the required files exist.

    When changes are uploaded to elastic search there is a delay in
    getting all the required log fixes into the system. This query returns
    facets for the failure on the filename, which ensures that we've
    found all the files in the system.
    """
    return generic('build_status:"FAILURE" '
                   'AND build_change:"%s" '
                   'AND build_patchset:"%s" '
                   'AND build_name:"%s" '
                   'AND build_short_uuid:%s' %
                   (review, patch, name, build_short_uuid),
                   facet='filename')


def single_patch(query, review, patch, build_short_uuid):
    """A query for a single patch (review + revision).

    This is used to narrow down a particular kind of failure found in a
    particular patch iteration.
    """
    return generic('%s '
                   'AND build_change:"%s" '
                   'AND build_patchset:"%s" '
                   'AND build_short_uuid:%s' %
                   (query, review, patch, build_short_uuid))


def most_recent_event():
    return generic(
        '(filename:"console.html" OR filename:"job-output.txt") '
        'AND (build_queue:gate OR build_queue:check) '
        'AND NOT tags:_grokparsefailure '
        'AND NOT message:"%{logmessage}" ')


def encode_logstash_query(query, timeframe=864000):
    """Utility function for encoding logstash queries.

    This is used when generating url's for links in
    report pages.

    Input is a string representing the logstash query
    and an optional timeframe argument.

    """
    timeframe = str(timeframe) + 's'
    # We need to first json encode the query so that
    # things like " are properly escaped. But we dont
    # want the outer ""s that result from dumpsing the
    # string so we strip those off again. And finally
    # the whole thing gets quoted to escape spaces and
    # special characters. Note that kibana3 is braindead
    # and expects its parameters url quoted not parameter
    # encoded
    query = json.dumps(query)[1:-1]
    return 'query=' + urlquote(query) + "&from=" + timeframe
