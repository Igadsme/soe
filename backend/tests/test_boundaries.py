import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import integrations, media, publishing, transcription


def result(stdout='', stderr=''):
    return SimpleNamespace(stdout=stdout, stderr=stderr)


def test_binary_lookup_and_fallback(monkeypatch):
    monkeypatch.setattr(media.shutil, 'which', lambda name: '/bin/'+name)
    assert media._binary('ffmpeg') == '/bin/ffmpeg'
    monkeypatch.setattr(media.shutil, 'which', lambda name: None)
    monkeypatch.setitem(sys.modules, 'imageio_ffmpeg', SimpleNamespace(get_ffmpeg_exe=lambda: '/bundled/ffmpeg'))
    assert media._binary('ffmpeg') == '/bundled/ffmpeg'
    with pytest.raises(media.MediaProcessingError):
        media._binary('ffprobe')
    monkeypatch.setitem(sys.modules, 'imageio_ffmpeg', None)
    with pytest.raises(media.MediaProcessingError):
        media._binary('ffmpeg')


def test_probe_branches(monkeypatch, tmp_path):
    monkeypatch.setattr(media.shutil, 'which', lambda _: '/bin/ffprobe')
    body = {'format': {'duration': '12.5'}, 'streams': [{'codec_type': 'video', 'width': 1280, 'height': 720}, {'codec_type': 'audio'}]}
    monkeypatch.setattr(media.subprocess, 'run', lambda *a, **k: result(json.dumps(body)))
    assert media.probe(tmp_path).duration == 12.5
    assert media.probe(tmp_path).has_audio
    body['streams'] = []
    with pytest.raises(media.MediaProcessingError):
        media.probe(tmp_path)
    monkeypatch.setattr(media.shutil, 'which', lambda _: None)
    monkeypatch.setattr(media, '_binary', lambda _: '/ffmpeg')
    monkeypatch.setattr(media.subprocess, 'run', lambda *a, **k: result(stderr='Duration: 01:02:03.50 Video: h264 1920x1080 Audio: aac'))
    assert media.probe(tmp_path) == media.MediaProbe(3723.5, 1920, 1080, True)
    monkeypatch.setattr(media.subprocess, 'run', lambda *a, **k: result(stderr='invalid'))
    with pytest.raises(media.MediaProcessingError):
        media.probe(tmp_path)


def test_ingest_render_and_scene(monkeypatch, tmp_path):
    source = tmp_path/'input.mp4'
    source.write_bytes(b'known input')
    probe = media.MediaProbe(20, 100, 100, True)
    monkeypatch.setattr(media, 'probe', lambda _: probe)
    destination, digest, metadata = media.ingest_authorized(str(source), tmp_path/'ingested'/'input.mp4')
    assert digest == media.sha256_file(source) and destination.read_bytes() == source.read_bytes() and metadata == probe
    with pytest.raises(media.MediaProcessingError):
        media.ingest_authorized(str(tmp_path/'missing'), destination)
    monkeypatch.setattr(media, '_binary', lambda _: '/ffmpeg')
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if command[-1] != '-': Path(command[-1]).write_bytes(b'output')
        return result(stderr='pts_time:12 pts_time:20')
    monkeypatch.setattr(media.subprocess, 'run', run)
    output = tmp_path/'output.mp4'
    media.render_vertical(source, output, 1, 4, tmp_path/'captions.srt')
    assert commands[0][:6] == ['/ffmpeg', '-y', '-ss', '1', '-to', '4']
    assert 'subtitles=' in commands[0][commands[0].index('-vf')+1]
    assert '+faststart' in commands[0] and 'loudnorm=' in ' '.join(commands[0])
    assert media.detect_scene_boundaries(source) == [12, 20]
    for start, end in [(2, 1), (0, 10000)]:
        with pytest.raises(media.MediaProcessingError): media.render_vertical(source, output, start, end)
    output.unlink()
    monkeypatch.setattr(media.subprocess, 'run', lambda *a, **k: result())
    with pytest.raises(media.MediaProcessingError): media.render_vertical(source, output, 0, 2)


def test_transcriber_fake_model(monkeypatch, tmp_path):
    calls = []
    class Model:
        def __init__(self, *args, **kwargs): calls.append((args, kwargs))
        def transcribe(self, audio, **kwargs):
            assert audio == str(tmp_path) and kwargs['word_timestamps']
            return [SimpleNamespace(start=0, end=1, text=' words '), SimpleNamespace(start=1, end=2, text=' ')], None
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Model))
    segments = transcription.FasterWhisperTranscriber('tiny').transcribe(tmp_path)
    assert segments[0].text == 'words' and len(segments) == 1
    assert calls[0] == (('tiny',), {'device': 'auto', 'compute_type': 'int8', 'download_root': transcription.settings.whisper_cache_dir})
    monkeypatch.setitem(sys.modules, 'faster_whisper', None)
    with pytest.raises(transcription.TranscriptionUnavailable): transcription.FasterWhisperTranscriber()


@pytest.fixture
def mock_http(monkeypatch):
    real_client = httpx.AsyncClient
    def install(handler):
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    return install


@pytest.mark.asyncio
async def test_official_adapters(mock_http, monkeypatch):
    monkeypatch.setattr(integrations.settings, 'youtube_api_key', 'test-key')
    monkeypatch.setattr(integrations.settings, 'twitch_client_id', 'test-id')
    monkeypatch.setattr(integrations.settings, 'twitch_client_secret', 'test-secret')
    requests = []
    def handler(request):
        requests.append(request)
        if 'oauth2' in str(request.url): return httpx.Response(200, json={'access_token': 'fake-token'})
        if 'googleapis' in str(request.url): return httpx.Response(200, json={'items': [{'id': {'videoId': 'abc'}, 'snippet': {'title': 'Title'}}]})
        assert request.headers['Authorization'] == 'Bearer fake-token'
        return httpx.Response(200, json={'data': [{'id': '123', 'display_name': 'Channel', 'broadcaster_login': 'channel'}]})
    mock_http(handler)
    assert (await integrations.YouTubeDataProvider().search('query'))[0].external_id == 'abc'
    assert (await integrations.TwitchHelixProvider().search('query'))[0].url.endswith('/channel')
    assert len(requests) == 3
    for code in [401, 429, 500]:
        mock_http(lambda request: httpx.Response(code))
        for provider in [integrations.YouTubeDataProvider(), integrations.TwitchHelixProvider()]:
            with pytest.raises(integrations.ProviderUnavailable): await provider.search('query')
    mock_http(lambda request: httpx.Response(200, json={}))
    with pytest.raises(integrations.ProviderUnavailable): await integrations.TwitchHelixProvider().search('query')


@pytest.mark.asyncio
async def test_publishers(mock_http, tmp_path):
    path = tmp_path/'clip.mp4'
    path.write_bytes(b'video')
    for platform, publisher, method, args in [
        ('youtube', publishing.YouTubePublisher(), 'upload', (path, 'title', 'description')),
        ('tiktok', publishing.TikTokPublisher(), 'initialize_direct_post', ('title', 5)),
        ('instagram', publishing.InstagramPublisher(), 'create_reel_container', ('https://example.com/video', 'caption')),
    ]:
        call = getattr(publisher, method)
        with pytest.raises(publishing.PublishingUnavailable): await call(publishing.OAuthAccount('wrong', '', ''), *args)
        for code in [401, 403]:
            mock_http(lambda request: httpx.Response(code))
            with pytest.raises(publishing.PublishingUnavailable): await call(publishing.OAuthAccount(platform, 'fake-token', 'id'), *args)
        mock_http(lambda request: httpx.Response(200, json={'id': 'published'}))
        response = await call(publishing.OAuthAccount(platform, 'fake-token', 'id'), *args)
        assert response == ('published' if platform == 'youtube' else {'id': 'published'})
    with pytest.raises(publishing.PublishingUnavailable):
        await publishing.YouTubePublisher().upload(publishing.OAuthAccount('youtube', '', ''), tmp_path/'missing', '', '')
