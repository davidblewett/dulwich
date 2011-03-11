from cStringIO import StringIO

class StringConsumer(object):

    def __init__(self):
        self._data = StringIO()

    def feed(self, data):
        self._data.write(data)

    def close(self):
        # We don't want to close the underlying StringIO instance
        return self._data

# The below courtesy of Fredrik Lundh
# http://effbot.org/zone/consumer-gzip.htm
# http://effbot.org/zone/copyright.htm , which contains this:
#   "Unless otherwise noted, source code can be be used freely.
#   Examples, test scripts and other short code fragments can be
#   considered as being in the public domain."
class GzipConsumer(object):
    """Consumer class to provide gzip decoding on the fly.
    The consumer acts like a filter, passing decoded data on to another
    consumer object.
    """
    def __init__(self, consumer=None):
        if consumer is None:
            consumer = StringConsumer()
        self._consumer = consumer
        self._decoder = None
        self._data = ''

    def feed(self, data):
        if self._decoder is None:
            # check if we have a full gzip header
            data = self._data + data
            try:
                i = 10
                flag = ord(data[3])
                if flag & 4: # extra
                    x = ord(data[i]) + 256*ord(data[i+1])
                    i = i + 2 + x
                if flag & 8: # filename
                    while ord(data[i]):
                        i = i + 1
                    i = i + 1
                if flag & 16: # comment
                    while ord(data[i]):
                        i = i + 1
                    i = i + 1
                if flag & 2: # crc
                    i = i + 2
                if len(data) < i:
                    raise IndexError('not enough data')
                if data[:3] != '\x1f\x8b\x08':
                    raise IOError('invalid gzip data')
                data = data[i:]
            except IndexError:
                self.__data = data
                return # need more data
            import zlib
            self._data = ''
            self._decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        data = self._decoder.decompress(data)
        if data:
            self._consumer.feed(data)

    def close(self):
        if self._decoder:
            data = self._decoder.flush()
            if data:
                self._consumer.feed(data)
        return self._consumer.close()

