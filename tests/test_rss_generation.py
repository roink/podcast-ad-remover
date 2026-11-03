from xml.etree.ElementTree import Element, SubElement, fromstring

import app.core.utils as utils
from app.core.config import settings
from app.core.rss_gen import _cdata, _get_feed_base_url, _serialize_rss


def test_serialize_rss_emits_html_description_as_cdata():
    description = (
        '<p>Hosted by <a href="https://example.com">Example</a></p>'
        '<p>Tom &amp; Jerry&nbsp;</p>'
    )
    rss = Element("rss")
    item = SubElement(rss, "item")
    SubElement(item, "description").text = _cdata(description)

    xml = _serialize_rss(rss)
    parsed_description = fromstring(xml).findtext("./item/description")

    assert parsed_description == description
    assert f"<![CDATA[{description}]]>" in xml
    assert "&lt;p&gt;Hosted by" not in xml


def test_serialize_rss_safely_round_trips_cdata_terminator_text():
    rss = Element("rss")
    item = SubElement(rss, "item")
    SubElement(item, "description").text = _cdata("before ]]> after")

    xml = _serialize_rss(rss)

    assert fromstring(xml).findtext("./item/description") == "before ]]> after"
    assert "<![CDATA[before ]]]]><![CDATA[> after]]>" in xml


def test_serialize_rss_restores_cdata_marker_after_serialization():
    rss = Element("rss")
    description = SubElement(SubElement(rss, "item"), "description")
    description.text = _cdata("<p>Notes</p>")

    _serialize_rss(rss)

    assert description.text == "<p>Notes</p>"
    assert type(description.text).__name__ == "_CDATA"


def test_feed_base_url_prefers_configured_external_url():
    assert _get_feed_base_url({"app_external_url": "https://podcasts.example.com/"}) == "https://podcasts.example.com"


def test_feed_base_url_preserves_existing_lan_fallback(monkeypatch):
    monkeypatch.setattr(settings, "BASE_URL", "http://localhost:9000")
    monkeypatch.setattr(utils, "get_lan_ip", lambda: "192.168.1.20")

    assert _get_feed_base_url({"app_external_url": ""}) == "http://192.168.1.20:9000"


def test_audio_enclosures_encode_filesystem_paths(monkeypatch, isolated_data_dir):
    from pathlib import Path
    from types import SimpleNamespace
    from urllib.parse import parse_qs, unquote, urlsplit

    from app.core.rss_gen import RSSGenerator
    from app.web.router import _append_feed_access_to_enclosures

    relative = 'lex-fridman-podcast/https:__lexfridman.com_?p=7807 #ü%2F/processed.mp3'
    episode = dict(
        title='Example', guid='stable-guid', original_url='https://example.com',
        pub_date=None, duration=60, local_filename=str(Path(settings.PODCASTS_DIR) / relative),
        file_size=123, description='Notes', subscription_id=1,
        podcast_slug='lex-fridman-podcast', podcast_title='Lex Fridman Podcast',
    )
    subscription = SimpleNamespace(
        id=1, slug='lex-fridman-podcast', title='Lex Fridman Podcast',
        feed_url='https://example.com/feed', description='', image_url=None,
        watermark_artwork=False,
    )
    monkeypatch.setattr(utils, 'get_global_settings', lambda: {'app_external_url': 'https://podcasts.example'})
    generator = RSSGenerator()
    generator.sub_repo = SimpleNamespace(get_by_id=lambda _: subscription, get_all=lambda: [subscription])
    generator.ep_repo = SimpleNamespace(
        get_completed_by_subscription=lambda _: [episode],
        get_completed_with_subscription_info=lambda user_id=None: [episode],
    )
    for output in (generator.generate_feed(1), generator.generate_unified_feed(), generator.generate_unified_feed(user_id=7)):
        xml = Path(output).read_text()
        url = fromstring(xml).find('./channel/item/enclosure').get('url')
        parts = urlsplit(url)
        assert parts.query == parts.fragment == ''
        assert unquote(parts.path) == '/audio/' + relative
        assert '%3F' in parts.path and '%252F' in parts.path
        authenticated = _append_feed_access_to_enclosures(xml, 'token', 'synthetic-token')
        url = fromstring(authenticated).find('./channel/item/enclosure').get('url')
        assert parse_qs(urlsplit(url).query) == {'token': ['synthetic-token']}
        assert unquote(urlsplit(url).path) == '/audio/' + relative
