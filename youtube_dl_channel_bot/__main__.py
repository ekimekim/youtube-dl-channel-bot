import logging
import sys
from youtube_dl_channel_bot.main import main

logging.basicConfig(level=logging.DEBUG)
ret = main(*sys.argv[1:])
sys.exit(ret)
