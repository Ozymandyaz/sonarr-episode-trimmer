#!/usr/bin/python
from operator import itemgetter
import requests
import urllib
import http.client
import json
import logging
import logging.handlers
import os
import configparser
import argparse
import sys

logging.basicConfig(level=logging.INFO,
                    format='%(levelname)-8s %(message)s',
                    stream=sys.stdout)

# setup weekly log file
log_path = os.path.join(os.path.dirname(__file__), 'logs')
log_file = os.path.join(log_path, 'sonarr-episode-trimmer.log')
if not os.path.exists(log_path):
    os.mkdir(os.path.dirname(log_file))
file_handler = logging.handlers.TimedRotatingFileHandler(log_file, when='D', interval=7, backupCount=4)
file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))
logging.getLogger().addHandler(file_handler)

httpclient_logger = logging.getLogger("http.client")

# def httpclient_logging_patch(level=logging.DEBUG):
#     """Enable HTTPConnection debug logging to the logging framework"""

#     def httpclient_log(*args):
#         httpclient_logger.log(level, " ".join(args))

#     # mask the print() built-in in the http.client module to use
#     # logging instead
#     http.client.print = httpclient_log
#     # enable debugging
#     http.client.HTTPConnection.debuglevel = 1

# httpclient_logging_patch()

def unmonitor_episode(episode):
    if not DEBUG:
        jsonBody = {"EpisodeIds": [episode['id']], "monitored" : False}
        api_request('episode/monitor' , method='PUT', body=jsonBody)

# make a request to the sonarr api
def api_request(action, params=None, method='GET', body=None):
    
    if params is None:
        params = {}

    params['apikey'] = CONFIG.get('API', 'key')

    url_base = CONFIG.get('API', 'url_base') if CONFIG.has_option('API', 'url_base') else ''
 
    url = "http://%s%s/api/v3/%s?%s" % (CONFIG.get('API', 'url'), url_base, action, urllib.parse.urlencode(params))

    if method == 'PUT':
        resp=requests.put(url=url, params=params, json=body)

    elif method == 'DELETE':
        resp=requests.delete(url)

    else:
        resp=requests.get(url, params)

    resp_body = resp.text
    if resp_body is None:
        resp_body = '{"Message": "none"}'

    if resp.status_code < 200 or resp.status_code > 299:
        logging.error('%s %s', resp.status_code, resp.reason)
        logging.error(resp_body)

    if resp_body is None:
        return ""

    else:    
        try: 
            return json.loads(resp_body)
        except ValueError as e:
            return ""

# remove old episodes from a series
def clean_series(series_id, keep_episodes):
    # get the episodes for the series
    all_episodes = api_request('episode', {'seriesId': series_id})

    # filter only downloaded episodes
    episodes = [episode for episode in all_episodes if episode['hasFile']]

    # sort episodes
    episodes = sorted(episodes, key=itemgetter('seasonNumber', 'episodeNumber'))

    logging.debug("# of episodes downloaded: %s", len(episodes))
    logging.debug("# of episodes to delete: %s", len(episodes[:-keep_episodes]))

    # filter monitored episodes
    monitored_episodes = [episode for episode in all_episodes if episode['monitored']]
    logging.debug("# of episodes monitored: %s", len(monitored_episodes))
    monitored_episodes = sorted(monitored_episodes, key=itemgetter('seasonNumber', 'episodeNumber'))

    # unmonitor episodes older than the last one downloaded
    # do this to keep older episodes that failed to download, from being searched for
    logging.info("Unmonitoring old episodes:")
    if len(episodes) > 0 and len(monitored_episodes) > 0:
        try:
            for episode in monitored_episodes[:monitored_episodes.index(episodes[0])]:
                unmonitor_episode(episode)

        except ValueError:
            logging.warning("There is an episode with a file that is unmonitored")

    # process episodes
    for episode in episodes[:-keep_episodes]:
        logging.info("Processing episode: %s", episode['title'])

        # get information about the episode's file
        episode_file = api_request('episodefile/%s' % episode['episodeFileId'])

        # delete episode
        logging.info("Deleting file: %s", episode_file['path'])
        if not DEBUG:
            api_request('episodefile/%s' % episode_file['id'], method='DELETE')

        #Unmonitor episode
        logging.info("Unmonitor file: %s", episode_file['path'])

        # mark the episode as unmonitored
        unmonitor_episode(episode)


if __name__ == '__main__':
    global CONFIG
    global DEBUG

    # parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action='store_true', help='Run the script in debug mode. No modifications to '
                                                             'the sonarr library or filesystem will be made.')
    parser.add_argument("--config", type=str, required=True, help='Path to the configuration file.')
    parser.add_argument("--list-series", action='store_true', help="Get a list of shows with their 'cleanTitle' for use"
                                                                   " in the configuration file")
    parser.add_argument("--custom-script", action='store_true', help="Run in 'Custom Script' mode. This mode is meant "
                                                                     "for adding the script to sonarr as a 'Custom "
                                                                     "Script'. It will run anytime a new episode is "
                                                                     "downloaded, but will only cleanup the series of "
                                                                     "the downloaded episode.")
    args = parser.parse_args()

    DEBUG = args.debug
    if DEBUG:
        logging.getLogger().setLevel(logging.DEBUG)

    # load config file
    CONFIG = configparser.ConfigParser()
    CONFIG.read(args.config)

    # get all the series in the library
    series = api_request('series')

    # print out a list of series
    if args.list_series:
        series = sorted(series, key=itemgetter('title'))
        for s in series:
            print( "%s: %s" % (s['title'], s['cleanTitle']))
    # cleanup series
    else:
        cleanup_series = []

        # custom script mode
        if args.custom_script:
            # verify it was a download event
            if os.environ['sonarr_eventtype'] == 'Download':
                series = {x['id']: x for x in series}
                config_series = {x[0]: x[1] for x in CONFIG.items('Series')}
                series_id = int(os.environ['sonarr_series_id'])
                # check if this episode is in a series in our config
                if series[series_id]['cleanTitle'] in config_series:
                    num_episodes = int(config_series[series[series_id]['cleanTitle']])
                    title = series[series_id]['title']

                    cleanup_series.append((series_id, num_episodes, title))
        # cronjob mode
        else:
            # build mapping of titles to series
            series = {x['cleanTitle']: x for x in series}

            for s in CONFIG.items('Series'):
                if s[0] in series:
                    cleanup_series.append((series[s[0]]['id'], int(s[1]), series[s[0]]['title']))
                else:
                    logging.warning("series '%s' from config not found in sonarr", s[0])

        for s in cleanup_series:
            logging.info("Processing: %s", s[2])
            logging.debug("%s: %s", s[0], s[1])
            clean_series(s[0], s[1])
