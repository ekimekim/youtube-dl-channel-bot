from setuptools import setup, find_packages

setup(
	name='youtube_dl_channel_bot',
	version='0.0.1',
	author='Mike Lang',
	author_email='mikelang3000@gmail.com',
	description='Bot for downloading new videos from a yt channel periodically',
	packages=find_packages(),
	install_requires=[
		"argh",
		"easycmd",
	],
)
