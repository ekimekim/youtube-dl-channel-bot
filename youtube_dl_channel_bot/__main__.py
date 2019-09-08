
import gevent.monkey
gevent.monkey.patch_all()

from argh import dispatch_command
from youtube_dl_channel_bot.main import main

dispatch_command(main)
