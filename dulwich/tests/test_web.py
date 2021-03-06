# test_web.py -- Tests for the git HTTP server
# Copyright (C) 2010 Google, Inc.
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

"""Tests for the Git HTTP server."""

from cStringIO import StringIO
import gzip
import os
import re
import shutil

from dulwich.tests.compat.utils import (
    import_repo_to_dir,
    )
from dulwich.log_utils import (
    getLogger
    )
from dulwich.object_store import (
    MemoryObjectStore,
    )
from dulwich.objects import (
    Blob,
    Tag,
    )
from dulwich.repo import (
    BaseRepo,
    MemoryRepo,
    )
from dulwich.server import (
    DictBackend,
    )
from dulwich.tests import (
    TestCase,
    SkipTest,
    )
from dulwich.web import (
    HTTP_OK,
    HTTP_NOT_FOUND,
    HTTP_FORBIDDEN,
    HTTP_ERROR,
    send_file,
    get_text_file,
    get_loose_object,
    get_pack_file,
    get_idx_file,
    get_info_refs,
    get_info_packs,
    handle_service_request,
    _LengthLimitedFile,
    GunzipFilter,
    LimitedInputFilter,
    HTTPGitRequest,
    HTTPGitApplication,
    )
from dulwich.web.paster import (
    make_app,
    make_gzip_filter,
    make_limit_input_filter,
)

from dulwich.tests.utils import (
    make_object,
    )

_BASE_PKG_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), os.pardir, os.pardir))

class TestHTTPGitRequest(HTTPGitRequest):
    """HTTPGitRequest with overridden methods to help test caching."""

    def __init__(self, *args, **kwargs):
        HTTPGitRequest.__init__(self, *args, **kwargs)
        self.cached = None

    def nocache(self):
        self.cached = False

    def cache_forever(self):
        self.cached = True


class WebTestCase(TestCase):
    """Base TestCase with useful instance vars and utility functions."""

    _req_class = TestHTTPGitRequest

    def setUp(self):
        super(WebTestCase, self).setUp()
        self._environ = {}
        self._req = self._req_class(self._environ, self._start_response,
                                    handlers=self._handlers())
        self._status = None
        self._headers = []
        self._output = StringIO()

    def _start_response(self, status, headers):
        self._status = status
        self._headers = list(headers)
        return self._output.write

    def _handlers(self):
        return None

    def assertContentTypeEquals(self, expected):
        self.assertTrue(('Content-Type', expected) in self._headers)


def _test_backend(objects, refs=None, named_files=None):
    if not refs:
        refs = {}
    if not named_files:
        named_files = {}
    repo = MemoryRepo.init_bare(objects, refs)
    for path, contents in named_files.iteritems():
        repo._put_named_file(path, contents)
    return DictBackend({'/': repo})


class DumbHandlersTestCase(WebTestCase):

    def test_send_file_not_found(self):
        list(send_file(self._req, None, 'text/plain'))
        self.assertEquals(HTTP_NOT_FOUND, self._status)

    def test_send_file(self):
        f = StringIO('foobar')
        output = ''.join(send_file(self._req, f, 'some/thing'))
        self.assertEquals('foobar', output)
        self.assertEquals(HTTP_OK, self._status)
        self.assertContentTypeEquals('some/thing')
        self.assertTrue(f.closed)

    def test_send_file_buffered(self):
        bufsize = 10240
        xs = 'x' * bufsize
        f = StringIO(2 * xs)
        self.assertEquals([xs, xs],
                          list(send_file(self._req, f, 'some/thing')))
        self.assertEquals(HTTP_OK, self._status)
        self.assertContentTypeEquals('some/thing')
        self.assertTrue(f.closed)

    def test_send_file_error(self):
        class TestFile(object):
            def __init__(self, exc_class):
                self.closed = False
                self._exc_class = exc_class

            def read(self, size=-1):
                raise self._exc_class()

            def close(self):
                self.closed = True

        f = TestFile(IOError)
        list(send_file(self._req, f, 'some/thing'))
        self.assertEquals(HTTP_ERROR, self._status)
        self.assertTrue(f.closed)
        self.assertFalse(self._req.cached)

        # non-IOErrors are reraised
        f = TestFile(AttributeError)
        self.assertRaises(AttributeError, list,
                          send_file(self._req, f, 'some/thing'))
        self.assertTrue(f.closed)
        self.assertFalse(self._req.cached)

    def test_get_text_file(self):
        backend = _test_backend([], named_files={'description': 'foo'})
        mat = re.search('.*', 'description')
        output = ''.join(get_text_file(self._req, backend, mat))
        self.assertEquals('foo', output)
        self.assertEquals(HTTP_OK, self._status)
        self.assertContentTypeEquals('text/plain')
        self.assertFalse(self._req.cached)

    def test_get_loose_object(self):
        blob = make_object(Blob, data='foo')
        backend = _test_backend([blob])
        mat = re.search('^(..)(.{38})$', blob.id)
        output = ''.join(get_loose_object(self._req, backend, mat))
        self.assertEquals(blob.as_legacy_object(), output)
        self.assertEquals(HTTP_OK, self._status)
        self.assertContentTypeEquals('application/x-git-loose-object')
        self.assertTrue(self._req.cached)

    def test_get_loose_object_missing(self):
        mat = re.search('^(..)(.{38})$', '1' * 40)
        list(get_loose_object(self._req, _test_backend([]), mat))
        self.assertEquals(HTTP_NOT_FOUND, self._status)

    def test_get_loose_object_error(self):
        blob = make_object(Blob, data='foo')
        backend = _test_backend([blob])
        mat = re.search('^(..)(.{38})$', blob.id)

        def as_legacy_object_error():
            raise IOError

        blob.as_legacy_object = as_legacy_object_error
        list(get_loose_object(self._req, backend, mat))
        self.assertEquals(HTTP_ERROR, self._status)

    def test_get_pack_file(self):
        pack_name = 'objects/pack/pack-%s.pack' % ('1' * 40)
        backend = _test_backend([], named_files={pack_name: 'pack contents'})
        mat = re.search('.*', pack_name)
        output = ''.join(get_pack_file(self._req, backend, mat))
        self.assertEquals('pack contents', output)
        self.assertEquals(HTTP_OK, self._status)
        self.assertContentTypeEquals('application/x-git-packed-objects')
        self.assertTrue(self._req.cached)

    def test_get_idx_file(self):
        idx_name = 'objects/pack/pack-%s.idx' % ('1' * 40)
        backend = _test_backend([], named_files={idx_name: 'idx contents'})
        mat = re.search('.*', idx_name)
        output = ''.join(get_idx_file(self._req, backend, mat))
        self.assertEquals('idx contents', output)
        self.assertEquals(HTTP_OK, self._status)
        self.assertContentTypeEquals('application/x-git-packed-objects-toc')
        self.assertTrue(self._req.cached)

    def test_get_info_refs(self):
        self._environ['QUERY_STRING'] = ''

        blob1 = make_object(Blob, data='1')
        blob2 = make_object(Blob, data='2')
        blob3 = make_object(Blob, data='3')

        tag1 = make_object(Tag, name='tag-tag',
                           tagger='Test <test@example.com>',
                           tag_time=12345,
                           tag_timezone=0,
                           message='message',
                           object=(Blob, blob2.id))

        objects = [blob1, blob2, blob3, tag1]
        refs = {
          'HEAD': '000',
          'refs/heads/master': blob1.id,
          'refs/tags/tag-tag': tag1.id,
          'refs/tags/blob-tag': blob3.id,
          }
        backend = _test_backend(objects, refs=refs)

        mat = re.search('.*', '//info/refs')
        self.assertEquals(['%s\trefs/heads/master\n' % blob1.id,
                           '%s\trefs/tags/blob-tag\n' % blob3.id,
                           '%s\trefs/tags/tag-tag\n' % tag1.id,
                           '%s\trefs/tags/tag-tag^{}\n' % blob2.id],
                          list(get_info_refs(self._req, backend, mat)))
        self.assertEquals(HTTP_OK, self._status)
        self.assertContentTypeEquals('text/plain')
        self.assertFalse(self._req.cached)

    def test_get_info_packs(self):
        class TestPack(object):
            def __init__(self, sha):
                self._sha = sha

            def name(self):
                return self._sha

        packs = [TestPack(str(i) * 40) for i in xrange(1, 4)]

        class TestObjectStore(MemoryObjectStore):
            # property must be overridden, can't be assigned
            @property
            def packs(self):
                return packs

        store = TestObjectStore()
        repo = BaseRepo(store, None)
        backend = DictBackend({'/': repo})
        mat = re.search('.*', '//info/packs')
        output = ''.join(get_info_packs(self._req, backend, mat))
        expected = 'P pack-%s.pack\n' * 3
        expected %= ('1' * 40, '2' * 40, '3' * 40)
        self.assertEquals(expected, output)
        self.assertEquals(HTTP_OK, self._status)
        self.assertContentTypeEquals('text/plain')
        self.assertFalse(self._req.cached)


class SmartHandlersTestCase(WebTestCase):

    class _TestUploadPackHandler(object):
        def __init__(self, backend, args, proto, http_req=None,
                     advertise_refs=False):
            self.args = args
            self.proto = proto
            self.http_req = http_req
            self.advertise_refs = advertise_refs

        def handle(self):
            self.proto.write('handled input: %s' % self.proto.recv(1024))

    def _make_handler(self, *args, **kwargs):
        self._handler = self._TestUploadPackHandler(*args, **kwargs)
        return self._handler

    def _handlers(self):
        return {'git-upload-pack': self._make_handler}

    def test_handle_service_request_unknown(self):
        mat = re.search('.*', '/git-evil-handler')
        list(handle_service_request(self._req, 'backend', mat))
        self.assertEquals(HTTP_FORBIDDEN, self._status)
        self.assertFalse(self._req.cached)

    def _run_handle_service_request(self, content_length=None):
        self._environ['wsgi.input'] = StringIO('foo')
        if content_length is not None:
            self._environ['CONTENT_LENGTH'] = content_length
        mat = re.search('.*', '/git-upload-pack')
        handler_output = ''.join(
          handle_service_request(self._req, 'backend', mat))
        write_output = self._output.getvalue()
        # Ensure all output was written via the write callback.
        self.assertEqual('', handler_output)
        self.assertEqual('handled input: foo', write_output)
        self.assertContentTypeEquals('application/x-git-upload-pack-result')
        self.assertFalse(self._handler.advertise_refs)
        self.assertTrue(self._handler.http_req)
        self.assertFalse(self._req.cached)

    def test_handle_service_request(self):
        self._run_handle_service_request()

    def test_handle_service_request_with_length(self):
        self._run_handle_service_request(content_length='3')

    def test_handle_service_request_empty_length(self):
        self._run_handle_service_request(content_length='')

    def test_get_info_refs_unknown(self):
        self._environ['QUERY_STRING'] = 'service=git-evil-handler'
        list(get_info_refs(self._req, 'backend', None))
        self.assertEquals(HTTP_FORBIDDEN, self._status)
        self.assertFalse(self._req.cached)

    def test_get_info_refs(self):
        self._environ['wsgi.input'] = StringIO('foo')
        self._environ['QUERY_STRING'] = 'service=git-upload-pack'

        mat = re.search('.*', '/git-upload-pack')
        handler_output = ''.join(get_info_refs(self._req, 'backend', mat))
        write_output = self._output.getvalue()
        self.assertEquals(('001e# service=git-upload-pack\n'
                           '0000'
                           # input is ignored by the handler
                           'handled input: '), write_output)
        # Ensure all output was written via the write callback.
        self.assertEquals('', handler_output)
        self.assertTrue(self._handler.advertise_refs)
        self.assertTrue(self._handler.http_req)
        self.assertFalse(self._req.cached)


class LengthLimitedFileTestCase(TestCase):
    def test_no_cutoff(self):
        f = _LengthLimitedFile(StringIO('foobar'), 1024)
        self.assertEquals('foobar', f.read())

    def test_cutoff(self):
        f = _LengthLimitedFile(StringIO('foobar'), 3)
        self.assertEquals('foo', f.read())
        self.assertEquals('', f.read())

    def test_multiple_reads(self):
        f = _LengthLimitedFile(StringIO('foobar'), 3)
        self.assertEquals('fo', f.read(2))
        self.assertEquals('o', f.read(2))
        self.assertEquals('', f.read())


class HTTPGitRequestTestCase(WebTestCase):

    # This class tests the contents of the actual cache headers
    _req_class = HTTPGitRequest

    def test_not_found(self):
        self._req.cache_forever()  # cache headers should be discarded
        message = 'Something not found'
        self.assertEquals(message, self._req.not_found(message))
        self.assertEquals(HTTP_NOT_FOUND, self._status)
        self.assertEquals(set([('Content-Type', 'text/plain')]),
                          set(self._headers))

    def test_forbidden(self):
        self._req.cache_forever()  # cache headers should be discarded
        message = 'Something not found'
        self.assertEquals(message, self._req.forbidden(message))
        self.assertEquals(HTTP_FORBIDDEN, self._status)
        self.assertEquals(set([('Content-Type', 'text/plain')]),
                          set(self._headers))

    def test_respond_ok(self):
        self._req.respond()
        self.assertEquals([], self._headers)
        self.assertEquals(HTTP_OK, self._status)

    def test_respond(self):
        self._req.nocache()
        self._req.respond(status=402, content_type='some/type',
                          headers=[('X-Foo', 'foo'), ('X-Bar', 'bar')])
        self.assertEquals(set([
          ('X-Foo', 'foo'),
          ('X-Bar', 'bar'),
          ('Content-Type', 'some/type'),
          ('Expires', 'Fri, 01 Jan 1980 00:00:00 GMT'),
          ('Pragma', 'no-cache'),
          ('Cache-Control', 'no-cache, max-age=0, must-revalidate'),
          ]), set(self._headers))
        self.assertEquals(402, self._status)


class HTTPGitApplicationTestCase(TestCase):

    def setUp(self):
        super(HTTPGitApplicationTestCase, self).setUp()
        self._app = HTTPGitApplication('backend')
        self._environ = {
            'PATH_INFO': '/foo',
            'REQUEST_METHOD': 'GET',
        }

    def _test_handler(self, req, backend, mat):
        # tests interface used by all handlers
        self.assertEquals(self._environ, req.environ)
        self.assertEquals('backend', backend)
        self.assertEquals('/foo', mat.group(0))
        return 'output'

    def _add_handler(self, app):
        req = self._environ['REQUEST_METHOD']
        app.services = {
          (req, re.compile('/foo$')): self._test_handler,
        }

    def test_call(self):
        self._add_handler(self._app)
        self.assertEquals('output', self._app(self._environ, None))

class GunzipTestCase(HTTPGitApplicationTestCase):
    """TestCase for testing the GunzipFilter, ensuring the wsgi.input
    is correctly decompressed and headers are corrected.
    """

    def setUp(self):
        super(GunzipTestCase, self).setUp()
        self._app = GunzipFilter(self._app)
        self._environ['HTTP_CONTENT_ENCODING'] = 'gzip'
        self._environ['REQUEST_METHOD'] = 'POST'

    def _get_zstream(self, text):
        zstream = StringIO()
        zfile = gzip.GzipFile(fileobj=zstream, mode='w')
        zfile.write(text)
        zfile.close()
        return zstream

    def test_call(self):
        self._add_handler(self._app.app)
        orig = self.__class__.__doc__
        zstream = self._get_zstream(orig)
        zlength = zstream.tell()
        zstream.seek(0)
        self.assertLess(zlength, len(orig))
        self.assertEquals(self._environ['HTTP_CONTENT_ENCODING'],
                          'gzip')
        self._environ['CONTENT_LENGTH'] = zlength
        self._environ['wsgi.input'] = zstream
        app_output = self._app(self._environ, None)
        buf = self._environ['wsgi.input']
        self.assertIsNot(buf, zstream)
        buf.seek(0)
        self.assertEquals(orig, buf.read())
        self.assertLess(zlength, int(self._environ['CONTENT_LENGTH']))
        self.assertNotIn('HTTP_CONTENT_ENCODING', self._environ)

class PasterFactoryTests(TestCase):
    """Tests for the Paster factory and filter functions."""

    def setUp(self):
        super(PasterFactoryTests, self).setUp()
        self.global_config = {'__file__': '/path/to/paster.ini'}
        self.repo_dirs = []
        self.repo_names = ('server_new.export', 'server_old.export')
        self.entry_points = {
            'main': make_app,
            'gzip': make_gzip_filter,
            'limitinput': make_limit_input_filter,
        }
        for rname in self.repo_names:
            self.repo_dirs.append(import_repo_to_dir(rname))
        # Test import to see if paste.deploy is available
        try:
            from paste.deploy.converters import asbool
            from pkg_resources import WorkingSet
            self.working_set = WorkingSet()
            self.working_set.add_entry(_BASE_PKG_DIR)
        except ImportError:
            raise SkipTest('paste.deploy not available')

    def tearDown(self):
        super(PasterFactoryTests, self).tearDown()
        for rdir in self.repo_dirs:
            shutil.rmtree(rdir)
        root = getLogger()
        if root.handlers:
            root.removeHandler(root.handlers[0])

    def test_cwd(self):
        cwd = os.getcwd()
        os.chdir(self.repo_dirs[0])
        app = make_app(self.global_config)
        os.chdir(cwd)
        self.assertIn('/', app.backend.repos)

    def test_badrepo(self):
        self.assertRaises(IndexError, make_app, self.global_config, foo='/')

    def test_repo(self):
        rname = self.repo_names[0]
        local_config = {rname: self.repo_dirs[0]}
        app = make_app(self.global_config, **local_config)
        self.assertIn('/%s' % rname, app.backend.repos)

    def _get_repo_parents(self):
        repo_parents = []
        for rdir in self.repo_dirs:
            repo_parents.append(os.path.split(rdir)[0])
        return repo_parents

    def test_append_git(self):
        app = make_app(self.global_config, append_git=True,
                       serve_dirs=self._get_repo_parents())
        for rname in self.repo_names:
            self.assertIn('/%s.git' % rname, app.backend.repos)

    def test_serve_dirs(self):
        app = make_app(self.global_config, serve_dirs=self._get_repo_parents())
        for rname in self.repo_names:
            self.assertIn('/%s' % rname, app.backend.repos)

    def _test_wrap(self, factory, wrapper):
        app = make_app(self.global_config, serve_dirs=self._get_repo_parents())
        wrapped_app = factory(self.global_config)(app)
        self.assertTrue(isinstance(wrapped_app, wrapper))

    def test_make_gzip_filter(self):
        self._test_wrap(make_gzip_filter, GunzipFilter)

    def test_make_limit_input_filter(self):
        self._test_wrap(make_limit_input_filter, LimitedInputFilter)

    def test_entry_points(self):
        test_points = {}
        for group in ('paste.app_factory', 'paste.filter_factory'):
            for ep in self.working_set.iter_entry_points(group):
                test_points[ep.name] = ep.load()

        for ep_name, ep in self.entry_points.items():
            self.assertTrue(test_points[ep_name] is ep)
