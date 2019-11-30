
import errno
import fcntl
import json
import logging
import os
import sys
import tempfile
import time
import urlparse

import argh
from easycmd import cmd

from .apiclient import GoogleAPIClient


class FLock(object):
	"""Context manager. Locks given file, creating it if it does not exist.
	Raises if lock cannot be taken. On graceful exit, deletes lock, but this is only
	for user clarity."""
	file = None

	def __init__(self, path):
		self.path = path

	def __enter__(self):
		self.lock()

	def __exit__(self, *exc_info):
		self.unlock()

	def lock(self):
		f = open(self.path, 'w')
		fcntl.flock(f.fileno(), fcntl.LOCK_EX)
		self.file = f

	def unlock(self):
		# note we delete then close, otherwise someone else could take the lock before we delete,
		# then a third party could take the lock again without the second party having released it.
		# actually that can still happen, if A opens, then B deletes, then B releases, then A acquires.
		# locking is hard.
		os.remove(self.path)
		self.file.close()


@argh.arg('--log', default='INFO')
@argh.arg('--conf', default='~/.youtube-dl-channel-bot.conf')
@argh.arg('--hook', default='~/.youtube-dl-channel-bot.hook')
@argh.arg('--lock', default='~/.youtube-dl-channel-bot.lock')
@argh.arg('--creds', default='~/.youtube-dl-channel-bot.creds')
@argh.arg('--filename-template', default='%(title)s-%(id)s.%(ext)s')
def main(*youtube_dl_args, **kwargs):
	log, conf, hook, lock, creds, filename_template = [
		kwargs[k] for k in ('log', 'conf', 'hook', 'lock', 'creds', 'filename_template')
	]
	conf, hook, lock, creds = [os.path.expanduser(s) for s in (conf, hook, lock, creds)]
	logging.basicConfig(level=log.upper(), format='%(levelname)s:%(asctime)s:%(process)d:%(name)s:%(message)s')
	logging.info("Executing with youtube-dl args: {!r}".format(youtube_dl_args))
	with FLock(lock):
		logging.info("Acquired lock {!r}".format(lock))
		channels = parse_conf(conf)
		# Do least recently checked first, in case we hit our api quota before finishing.
		channels.sort(key=lambda (u,p,t): 0 if t is None else t)
		logging.info("Got config for {} channels".format(len(channels)))
		with open(creds) as f:
			creds = json.load(f)
		client = GoogleAPIClient(base_url='https://www.googleapis.com/youtube/v3', **creds)
		for url, path, timestamp in channels:
			start_time = time.time()
			logging.info("Checking for new videos from {!r}".format(url))
			new_files = check_url(client, url, path, youtube_dl_args, filename_template)
			update_conf(conf, {(url, path): start_time})
			logging.info("Got {} new files".format(len(new_files)))
			if new_files:
				if os.access(hook, os.X_OK):
					logging.info("Calling hook {!r}".format(hook))
					cmd([hook], stdin='\n'.join(new_files)+'\n')
				else:
					logging.info("Hook {!r} does not exist or is not executable".format(hook))
			logging.info("Ran for entry {}".format(url))
		logging.info("Ran successfully")


def parse_conf(path):
	"""Parse conf file and return a list of (url, path, timestamp) where timestamp may be None"""
	return [item for item in _parse_conf(path) if not isinstance(item, basestring)]

def _parse_conf(path):
	"""As parse_conf, but additionally returns malformed lines, in correct position, as a raw string."""
	items = []
	with open(path) as f:
		lines = f.read().split('\n')
	for i, line in enumerate(lines):
		if not line:
			continue
		parts = line.split('\t')
		if len(parts) not in (2, 3):
			logging.warning(
				"Bad line {} in conf file: Wrong number of parts ({})".format(i+1, len(parts))
			)
			continue
		if len(parts) == 2:
			url, path = parts
			timestamp = None
		else:
			timestamp, url, path = parts
			if not timestamp.isdigit():
				logging.warning(
					"Bad line {} in conf file: Bad timestamp {!r}".format(i+1, timestamp)
				)
			timestamp = int(timestamp)
		items.append((url, path, timestamp))
	return items


def update_conf(path, update_times):
	"""Update conf with new timestamps. If conf has since changed, we attempt to reconcile differences
	by only updating timestamps where url and path both match.
	update_times should be a dict {(url, path): timestamp}.
	"""
	# Note there's still potential for a race here if conf file is edited after we read it but
	# before we replace it, but it's impossible to fully remove without cooperative locking and
	# very unlikely to happen in practice.
	new_conf = []
	for item in _parse_conf(path):
		if isinstance(item, basestring):
			new_conf.append(item)
			continue
		url, item_path, old_ts = item
		new_ts = update_times.get((url, item_path), old_ts)
		if new_ts is None:
			new_conf.append('\t'.join([url, item_path]))
		else:
			new_conf.append('\t'.join([str(int(new_ts)), url, item_path]))
	# note we use a temp path so we can use os.rename for atomic switch (no partial writes on crash)
	tmp_path = "{}.tmp".format(path)
	logging.debug("writing new config to {!r}".format(tmp_path))
	with open(tmp_path, 'w') as f:
		f.write('\n'.join(new_conf) + '\n')
	os.rename(tmp_path, path)


def check_url(youtube_client, url, path, youtube_dl_args, filename_template):
	"""For given playlist, user or channel url,
	checks for videos that don't already exist, and if so downloads them to
	given path. Adds any given youtube-dl args as extra args. Returns a list of new files."""

	parsed = urlparse.urlparse(url)
	query = urlparse.parse_qs(parsed.query)
	if parsed.path.startswith('/playlist'):
		playlist = query['list']
		logging.info("Interpreted url {} as playlist {}".format(url, playlist))
	else:
		if parsed.path.startswith('/user'):
			user = parsed.path.split('/')[2]
			params = {'forUsername': user}
		elif parsed.path.startswith('/channel'):
			channel = parsed.path.split('/')[2]
			params = {'id': channel}
		else:
			raise ValueError("Unrecognised url {!r}".format(url))
		result = youtube_client.request('GET', 'channels', part='contentDetails', **params)
		if not result['items']:
			raise ValueError("No channel found for {}".format(params))
		if len(result['items']) > 1:
			raise ValueError("Multiple channels found for {}: {}".format(params, result['items']))
		item = result['items'][0]
		playlist = item['contentDetails']['relatedPlaylists']['uploads']
		logging.info("Interpreted url {} as {}, got uploads playlist {}".format(url, params, playlist))

	logging.info("Looking for new videos in playlist {}".format(playlist))

	# download list of items in playlist
	items = []
	token = None
	while True:
		result = youtube_client.request('GET', 'playlistItems',
			playlistId=playlist, part='snippet',
			**({'pageToken': token} if token is not None else {})
		)
		items += result['items']
		if 'nextPageToken' not in result:
			break
		token = result['nextPageToken']

	logging.info("Found {} videos in playlist".format(len(items)))

	items = [item['snippet']['resourceId']['videoId'] for item in items]

	try:
		os.makedirs(path)
	except OSError as e:
		if e.errno != errno.EEXIST:
			raise

	exists = os.listdir(path)
	to_download = []
	for id in items:
		# ignore ones that already exist
		matches = [name for name in exists if "-{}.".format(id) in name]
		if matches:
			logging.debug("Ignoring video {}: already exists as {}".format(id, matches))
			continue
		to_download.append(id)

	if not to_download:
		return []

	logging.info("Downloading videos: {}".format(to_download))

	# In order to get a list of downloaded files, we resort to a hack:
	# we download to a temp dir first, then rename.
	# This also protects us from partial downloads.
	tempdir = tempfile.mkdtemp(prefix='youtube-dl-channel-bot-', suffix='.tmp.d', dir=path)
	try:
		output_template = '{}/{}'.format(tempdir, filename_template)
		# Unfortunately, youtube-dl will exit 1 if there are any copyright-blocked videos,
		# even with --ignore-errors. We allow 1 as a success exit code.
		cmd(
			['youtube-dl', '--ignore-errors'] + list(youtube_dl_args)
			+ ['-o', output_template, '--'] + to_download,
			stdout=sys.stdout,
			success=[0,1],
		)
		ret = []
		for name in os.listdir(tempdir):
			new_path = os.path.join(path, name)
			logging.info("Saving new file {!r}".format(new_path))
			os.rename(os.path.join(tempdir, name), new_path)
			ret.append(new_path)
		return ret
	finally:
		# attempt to clean up tempdir as much as we can
		for name in os.listdir(tempdir):
			try:
				os.remove(os.path.join(tempdir, name))
			except EnvironmentError:
				pass
		try:
			os.rmdir(tempdir)
		except EnvironmentError:
			pass
