import unittest

from extract_cursor_cli import parse_ref_list


class ParseRefListTests(unittest.TestCase):
    def test_empty_and_incomplete_frames_have_no_references(self):
        self.assertEqual(parse_ref_list(b''), [])
        self.assertEqual(parse_ref_list(b'\x0a'), [])
        self.assertEqual(parse_ref_list(b'\x0a\x20' + b'x' * 31), [])

    def test_preserves_order_across_noise_and_binary_unicode_bytes(self):
        first = bytes(range(32))
        second = '\u2603'.encode('utf-8') * 10 + b'xy'
        data = b'prefix\x0a\x1fignored' + b'\x0a\x20' + first + b'noise' + b'\x0a\x20' + second
        self.assertEqual(parse_ref_list(data), [first.hex(), second.hex()])

    def test_accepts_mutable_binary_input(self):
        ref = bytearray(range(32))
        self.assertEqual(parse_ref_list(bytearray(b'\x0a\x20') + ref), [bytes(ref).hex()])


if __name__ == '__main__':
    unittest.main()
