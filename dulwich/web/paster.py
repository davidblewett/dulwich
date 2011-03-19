# paster.py -- WSGI smart-http server
# Copyright (C) 2011 David Blewett
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; version 2
# or (at your option) any later version of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.

import os

from dulwich import log_utils
from dulwich.errors import NotGitRepository
from dulwich.repo import Repo
from dulwich.server import DictBackend
from dulwich.web import (
    GunzipFilter,
    HTTPGitApplication,
    LimitedInputFilter,
)

logger = log_utils.getLogger(__name__)


def make_app(global_config, **local_conf):
    """Factory function for a Paster WSGI app
    append_git=True will make each served git repo have .git appended to its
        served URL.

    Two options to serve: serve_dirs and individual URL path to operating
    system path mappings.
    Example:
        File-system layout:
            +-/var/lib/git
            |-foo
            |-bar
            `-baz

            +-/home/git
            |-bing
            `-bang

        paster.ini:
            [app:main]
            use = egg:dulwich
            append_git = True
            serve_dirs =
                /var/lib/git
                /home/git
            blerg = /home/dannyboy/src/blerg

    Will result in the following being served:
    /foo.git   => /var/lib/git/foo
    /bar.git   => /var/lib/git/bar
    /baz.git   => /var/lib/git/baz
    /bing.git  => /home/git/bing
    /bang.git  => /home/git/bang
    /blerg.git => /home/dannyboy/src/blerg

    NOTE: The last name definition wins. Whatever directory in serve_dirs is
          last, or the last explicit mapping for the same name is what will
          be mapped.
    """
    from paste.deploy.converters import asbool
    from paste.deploy.converters import aslist
    repos = {}
    append_git = asbool(local_conf.pop('append_git', False))
    serve_dirs = aslist(local_conf.pop('serve_dirs', None))
    log_utils.default_logging_config()

    def add_repo(mapping, path, gitdir):
        try:
            mapping[path] = Repo(gitdir)
        except NotGitRepository:
            logger.error('Not a git repository, cannot serve: "%s".',
                         gitdir)

    if not local_conf and not serve_dirs:
        add_repo(repos, '/', os.getcwd())
    else:
        if serve_dirs:
            for top_dir in serve_dirs:
                if not os.path.isdir(top_dir):
                    logger.error('Not a directory, cannot serve: "%s".',
                                 top_dir)

                for d in os.listdir(top_dir):
                    repo_path = '/'.join(('', d))
                    gitdir = os.path.join(top_dir, d)
                    add_repo(repos, repo_path, gitdir)

        for repo_name, gitdir in local_conf.items():
            repo_path = '/'.join(('', repo_name))
            add_repo(repos, repo_path, gitdir)

    if not repos:
        msg = 'No repositories to serve, check the ini file: "%s".'
        logger.error(msg, global_config['__file__'])
        raise IndexError(msg % global_config['__file__'])

    if append_git:
        new_repos = {}
        for rpath, repo in repos.items():
            if rpath.endswith('.git'):
                # Don't be redundant...
                new_repos[rpath] = repo
                logger.debug('Not renaming, already ends in .git: "%s".',
                             rpath)
            else:
                new_repos['.'.join((rpath, 'git'))] = repo
        backend = DictBackend(new_repos)
    else:
        backend = DictBackend(repos)
    return HTTPGitApplication(backend)


def make_gzip_filter(global_config):
    """Factory function to wrap a given WSGI application in a GunzipFilter,
    to transparently decode Content-Encoding: gzip requests.
    """
    return GunzipFilter


def make_limit_input_filter(global_config):
    return LimitedInputFilter
