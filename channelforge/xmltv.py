"""Combine XMLTV guides from multiple sources into one file, filtered to the
channel ids in use. Streams to disk — memory stays bounded no matter how many
days of programmes the source guides carry."""
import gzip
import io
import xml.etree.ElementTree as ET


def _gunzip(blob):
    if blob[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(blob)
        except OSError:
            return b""
    return blob


def _stream_matching(blob, tag, wanted_ids, id_attr, out, seen=None):
    """Write every <tag> element whose id attribute is wanted straight to `out`."""
    src_root = None
    try:
        for event, elem in ET.iterparse(io.BytesIO(blob), events=("start", "end")):
            if event == "start":
                if src_root is None:
                    src_root = elem
                continue
            if elem.tag == tag:
                key = elem.get(id_attr, "")
                if key in wanted_ids and (seen is None or key not in seen):
                    if seen is not None:
                        seen.add(key)
                    out.write(ET.tostring(elem, encoding="utf-8"))
                src_root.clear()
            elif elem.tag in ("channel", "programme"):
                src_root.clear()
    except ET.ParseError:
        pass


def write_combined(xml_blobs, wanted_ids, out_path):
    """xml_blobs: list of raw bytes (possibly gzipped) XMLTV documents.
    Writes the merged, filtered guide to out_path. Returns number of channels kept."""
    blobs = [_gunzip(b) for b in xml_blobs if b]
    seen_channels = set()
    with open(out_path, "wb") as out:
        out.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
        out.write(b'<tv generator-info-name="channelforge">\n')
        for blob in blobs:                # channels first, per the XMLTV convention
            _stream_matching(blob, "channel", wanted_ids, "id", out, seen_channels)
        for blob in blobs:
            _stream_matching(blob, "programme", wanted_ids, "channel", out)
        out.write(b"</tv>\n")
    return len(seen_channels)
