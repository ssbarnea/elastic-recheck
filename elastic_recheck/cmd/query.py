#!/usr/bin/env python

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

import argparse
import itertools
import json
import yaml

import elastic_recheck.config as er_conf
import elastic_recheck.log as logging
import elastic_recheck.results as er_results

LOG = logging.getLogger('erquery')

DEFAULT_NUMBER_OF_DAYS = 10
DEFAULT_MAX_QUANTITY = 5
IGNORED_ATTRIBUTES = [
    'build_master',
    'build_patchset',
    'build_ref',
    'build_short_uuid',
    'build_uuid',
    'error_pr',
    'host',
    'received_at',
    'type',
]


def analyze_attributes(attributes):
    analysis = {}
    for attribute, values in attributes.items():
        if attribute[0] == '@' or attribute == 'message':
            # skip meta attributes and raw messages
            continue

        analysis[attribute] = []

        total_hits = sum(values.values())
        for value_hash, hits in values.items():
            value = json.loads(value_hash)
            analysis[attribute].append((100.0 * hits / total_hits, value))

        # sort by hit percentage descending, and then by value ascending
        analysis[attribute] = sorted(
            analysis[attribute],
            key=lambda x: (1 - x[0], x[1]))

    return analysis


def query(query_file_name, config=None, days=DEFAULT_NUMBER_OF_DAYS,
          quantity=DEFAULT_MAX_QUANTITY, verbose=False,):
    _config = config or er_conf.Config()
    es = er_results.SearchEngine(url=_config.es_url,
                                 indexfmt=_config.es_index_format)

    with open(query_file_name) as f:
        query_file = yaml.load(f.read())
        query = query_file['query']

    r = es.search(query, days=days)
    print('total hits: %s' % r.hits['total'])

    attributes = {}

    for hit in r.hits['hits']:
        for key, value in hit['_source'].items():
            value_hash = json.dumps(value)
            attributes.setdefault(key, {}).setdefault(value_hash, 0)
            attributes[key][value_hash] += 1

    analysis = analyze_attributes(attributes)
    for attribute, results in sorted(analysis.items()):
        if not verbose and attribute in IGNORED_ATTRIBUTES:
            # skip less-than-useful attributes to reduce noise in the report
            continue

        print(attribute)
        for percentage, value in itertools.islice(results, quantity):
            if isinstance(value, list):
                value = ' '.join(str(x) for x in value)
            print('  %d%% %s' % (percentage, value))


def main():
    parser = argparse.ArgumentParser(
        description='Execute elastic-recheck query files and analyze the '
                    'results.')
    parser.add_argument(
        'query_file', type=argparse.FileType('r'),
        help='Path to an elastic-recheck YAML query file.')
    parser.add_argument(
        '--quantity', '-q', type=int, default=DEFAULT_MAX_QUANTITY,
        help='Maximum quantity of values to show for each attribute.')
    parser.add_argument(
        '--days', '-d', type=int, default=DEFAULT_NUMBER_OF_DAYS,
        help='Timespan to query, in days.')
    parser.add_argument(
        '--verbose', '-v', action='store_true', default=False,
        help='Report on additional query metadata.')
    parser.add_argument('-c', '--conf', help="Elastic Recheck Configuration "
                        "file to use for data_source options such as "
                        "elastic search url, logstash url, and database uri.")
    args = parser.parse_args()

    config = er_conf.Config(config_file=args.conf)

    query(args.query_file.name, config=config, days=args.days,
          quantity=args.quantity, verbose=args.verbose)


if __name__ == "__main__":
    main()
