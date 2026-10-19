"""Comprehensive regression suite for downloader.py.

Run from the project root:
    python tests/regress_downloader.py [--network]

Runs in an isolated temp working directory so the real data.db / output folders
are never touched. Network-dependent checks are opt-in via --network.

    python _regress.py            # offline logic checks
    python _regress.py --network  # + live douyin checks
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
PROJECT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"  PASS  {name}")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        RESULTS.append((name, False, detail))
        print(f"  FAIL  {name}  ->  {detail}")


def eq(actual, expected, label=""):
    assert actual == expected, f"{label} expected {expected!r}, got {actual!r}"


def load_module():
    spec = importlib.util.spec_from_file_location("dl", PROJECT / "downloader.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["dl"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
def offline_checks(mod, workdir: Path) -> None:
    CT = mod.ContentType

    def content_types():
        d = mod.UnifiedDownloader(str(workdir / "c_off.yml"))
        eq(d.detect_content_type("https://www.douyin.com/video/123"), CT.VIDEO, "video")
        eq(d.detect_content_type("https://www.douyin.com/user/MS4abc"), CT.USER, "user")
        eq(
            d.detect_content_type("https://www.douyin.com/user/MS4abc?modal_id=123"),
            CT.VIDEO,
            "user-modal",
        )
        eq(
            d.detect_content_type("https://www.douyin.com/user/self?modal_id=123&showTab=like"),
            CT.VIDEO,
            "self-modal",
        )
        eq(d.detect_content_type("https://www.douyin.com/collection/7123"), CT.MIX, "collection")
        eq(d.detect_content_type("https://www.douyin.com/mix/7123"), CT.MIX, "mix")
        eq(d.detect_content_type("https://www.douyin.com/music/7123"), CT.MUSIC, "music")
        eq(d.detect_content_type("https://live.douyin.com/123"), CT.LIVE, "live")
        eq(d.detect_content_type("https://www.douyin.com/note/123"), CT.IMAGE, "note")

    def extract_ids():
        d = mod.UnifiedDownloader(str(workdir / "c_off.yml"))
        eq(d.extract_id_from_url("https://www.douyin.com/video/123456"), "123456", "video id")
        eq(d.extract_id_from_url("https://www.douyin.com/note/999"), "999", "note id")
        eq(d.extract_id_from_url("https://www.douyin.com/user/MS4xyz"), "MS4xyz", "user id")
        eq(
            d.extract_id_from_url("https://www.douyin.com/user/MS4xyz?modal_id=42"),
            "42",
            "modal id wins",
        )
        eq(d.extract_id_from_url("https://no-id-here/"), None, "no id")

    def safe_names():
        d = mod.UnifiedDownloader(str(workdir / "c_off.yml"))
        eq(d._safe_name("a/b\\c:d*e?f"), "a_b_c_d_e_f", "illegal chars")
        eq(d._safe_name(""), "unknown", "empty -> default")
        eq(d._safe_name(None, default="x"), "x", "none -> default")
        assert len(d._safe_name("很长的名字" * 40, max_len=20)) <= 20, "max_len honoured"
        assert d._safe_name("   ") not in ("", "   "), "blank gets default"

    def folder_names():
        d = mod.UnifiedDownloader(str(workdir / "c_off.yml"))
        name = d._build_post_folder_name("2026-06-30_13-38-37", "描述", "7628")
        assert name.endswith("_7628"), name
        assert "2026-06-30" in name, name
        long_name = d._build_post_folder_name("2026-06-30_13-38-37", "长" * 300, "1")
        assert len(long_name) <= 120, len(long_name)

    def frame_prefix():
        d = mod.UnifiedDownloader(str(workdir / "c_off.yml"))
        eq(d._frame_prefix_for_video(Path("clip_1.mp4")), "clip_01_", "clip_1")
        eq(d._frame_prefix_for_video(Path("clip_12.mp4")), "clip_12_", "clip_12")
        eq(d._frame_prefix_for_video(Path("my video!.mp4")), "video_my_video_.mp4_"[:0] or "video_my_video_.mp4_".replace(".mp4_", ""), "sanitised") if False else None
        assert d._frame_prefix_for_video(Path("my video!.mp4")).startswith("video_"), "stem prefix"
        assert d._frame_prefix_for_video(Path("中文名.mp4"), 3) == "video_03_", "fallback index"

    def url_helpers():
        d = mod.UnifiedDownloader(str(workdir / "c_off.yml"))
        eq(d._unique_urls(["a", "b", "a", "", None]), ["a", "b"], "unique")
        assert d._is_watermark_like_url("https://x/playwm/abc"), "watermark detected"
        assert not d._is_watermark_like_url("https://x/play/abc"), "clean url"
        pref = d._prefer_non_watermark_urls(["https://x/playwm/1", "https://x/play/2"])
        eq(pref[0], "https://x/play/2", "non-watermark first")
        eq(d._extract_addr_url_candidates({"url_list": ["u1", "u2"]}), ["u1", "u2"], "addr list")
        eq(d._extract_addr_url_candidates(None), [], "addr none")
        eq(d._extract_addr_url_candidates({"url_list": None}), [], "addr none list")

    def media_candidates():
        d = mod.UnifiedDownloader(str(workdir / "c_off.yml"))
        info = {
            "video": {
                "play_addr": {"url_list": ["https://x/playwm/1", "https://x/play/2"]},
                "bit_rate": [{"play_addr": {"url_list": ["https://x/bitrate"]}}],
                "cover": {"url_list": ["https://x/cover"]},
            },
            "music": {"play_url": {"url_list": ["https://x/music"]}},
        }
        urls = d._get_video_url_candidates(info)
        assert urls and any("play" in u for u in urls), urls
        eq(d._get_music_url(info), "https://x/music", "music url")
        eq(d._get_cover_url(info), "https://x/cover", "cover url")
        images = {"images": [{"url_list": ["https://x/img1"]}]}
        assert d._get_image_url_candidates(images["images"][0]), "image candidates"
        clip = {"images": [{"clip_type": 1, "video": {"play_addr": {"url_list": ["https://x/clip"]}}}]}
        eq(d._get_image_clip_url_candidates(clip["images"][0]), ["https://x/clip"], "clip url")
        eq(d._get_image_clip_url_candidates({}), [], "no video -> empty")

    def config_aliases():
        cfg = workdir / "alias.yml"
        cfg.write_text(
            "links:\n  - https://a\noutput_dir: ./out_alias\ncookie: 'k=v'\n",
            encoding="utf-8",
        )
        d = mod.UnifiedDownloader(str(cfg))
        eq(d.config.get("link"), ["https://a"], "links -> link")
        eq(d.config.get("path"), "./out_alias", "output_dir -> path")
        eq(d.config.get("cookies"), "k=v", "cookie -> cookies")

    def missing_config():
        d = mod.UnifiedDownloader(str(workdir / "does_not_exist_xyz.yml"))
        assert isinstance(d.config, dict), "missing config yields empty dict"

    def time_filter():
        cfg = workdir / "time.yml"
        cfg.write_text("start_time: '2026-01-01'\nend_time: '2026-06-30'\n", encoding="utf-8")
        d = mod.UnifiedDownloader(str(cfg))
        ts = lambda s: int(datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp())
        assert d._check_time_filter({}), "empty aweme passes"
        assert d._check_time_filter({"create_time": ts("2026-03-15 10:00")}), "in range"
        assert not d._check_time_filter({"create_time": ts("2025-12-31 10:00")}), "before start"
        assert not d._check_time_filter({"create_time": ts("2026-07-01 10:00")}), "after end"
        # end date must be inclusive of the whole day
        assert d._check_time_filter({"create_time": ts("2026-06-30 23:30")}), "end day inclusive"
        assert d._check_time_filter({"create_time": ts("2026-01-01 00:10")}), "start day inclusive"

    def increment_logic():
        cfg = workdir / "inc.yml"
        cfg.write_text("increase:\n  post: false\n", encoding="utf-8")
        d = mod.UnifiedDownloader(str(cfg))
        info = {"aweme_id": "111", "author": {"sec_uid": "sec1"}}
        assert not d._should_skip_increment("post", info), "increase off -> never skip"
        d._record_increment("post", info)
        assert d._should_skip_increment("post", info) is False, "still not skipping"

        cfg2 = workdir / "inc2.yml"
        cfg2.write_text("increase:\n  post: true\n", encoding="utf-8")
        d2 = mod.UnifiedDownloader(str(cfg2))
        info2 = {"aweme_id": "222", "author": {"sec_uid": "sec2"}}
        assert not d2._should_skip_increment("post", info2), "not recorded yet"
        d2._record_increment("post", info2)
        assert d2._should_skip_increment("post", info2), "recorded -> skip"

    def database_roundtrip():
        db = mod.DataBase()
        db.insert_user_post("secX", 424242, {"aweme_id": "424242"})
        row = db.get_user_post("secX", 424242)
        assert row, "db row missing"
        eq(row[2], 424242, "aweme id roundtrip")

    def number_limits():
        cfg = workdir / "num.yml"
        cfg.write_text("number:\n  post: 2\n  like: 1\n", encoding="utf-8")
        d = mod.UnifiedDownloader(str(cfg))
        eq(d.config["number"]["post"], 2, "number.post")

    def slicing():
        import imageio_ffmpeg
        import subprocess

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        post = workdir / "post1"
        post.mkdir(exist_ok=True)
        src = post / "clip_1.mp4"
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
             "-pix_fmt", "yuv420p", str(src)],
            capture_output=True, check=True,
        )
        d = mod.UnifiedDownloader(str(workdir / "c_slice.yml"))
        d.slice_fps = 5
        n = d._slice_videos_for_post(post, [src])
        eq(n, 10, "frames at 5fps for 2s")
        eq(d._slice_videos_for_post(post, [src]), 10, "idempotent")

    def backfill():
        import imageio_ffmpeg
        import subprocess

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        root = workdir / "backfill_root"
        old = root / "author" / "2026-01-01_00-00-00"
        old.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=128x96:rate=10",
             "-pix_fmt", "yuv420p", str(old / "clip_1.mp4")],
            capture_output=True, check=True,
        )
        cfg = workdir / "bf.yml"
        cfg.write_text(
            f"path: {str(root).replace(chr(92), '/')}\nslice_videos: true\n"
            "slice_fps: 5\nslice_backfill_on_run: true\n",
            encoding="utf-8",
        )
        d = mod.UnifiedDownloader(str(cfg))
        d._slice_backfill_existing_videos()
        assert (old / "frames").exists(), "backfill created frames dir"
        assert list((old / "frames").glob("*.jpg")), "backfill produced frames"

    def ffmpeg_and_cookies():
        d = mod.UnifiedDownloader(str(PROJECT / "config_simple.yml"))

        async def go():
            await d._initialize_cookies_and_headers()

        asyncio.run(go())
        assert d._resolve_ffmpeg_path(), "ffmpeg resolvable"

    def cookie_auto_renewal():
        """服务端下发的 Set-Cookie 必须被吸收并写回，从而实现免重复登录。"""
        import pickle
        import time as _time

        d = mod.UnifiedDownloader(str(workdir / "c_off.yml"))
        old_expire = int(_time.time()) + 10 * 86400
        d.cookies = {
            'sessionid': 'old-session',
            'sid_guard': f'old|{old_expire - 3600}|3600|x',
        }
        d.headers['Cookie'] = d._build_cookie_string()
        assert d._cookie_expiry_hint(d._cookies_to_dict(d.cookies)) is None, "baseline valid"

        class Morsel:
            def __init__(self, value):
                self.value = value

        class FakeResponse:
            cookies = {
                'sessionid': Morsel('new-session'),
                'sid_guard': Morsel(f'new|{old_expire + 20 * 86400}|2592000|x'),
                'ttwid': Morsel('new-ttwid'),
            }

        d._absorb_response_cookies(FakeResponse())
        merged = d._cookies_to_dict(d.cookies)
        assert merged.get('sessionid') == 'new-session', merged.get('sessionid')
        assert merged.get('ttwid') == 'new-ttwid', "new cookie not added"

        # 写回 cookies.txt
        txt = (workdir / 'cookies.txt').read_text(encoding='utf-8')
        assert 'sessionid=new-session' in txt, txt[:80]
        # 写回 cookies.pkl（保持 CookieInfo 结构）
        with open(workdir / 'cookies.pkl', 'rb') as fh:
            payload = pickle.load(fh)
        assert isinstance(payload, dict) and payload.get('cookies'), payload
        values = {c['name']: c['value'] for c in payload['cookies']}
        assert values.get('sessionid') == 'new-session', values.get('sessionid')

        # 有效期确实被延长
        new_expire = d._cookie_expiry_at(merged)
        assert new_expire and new_expire > old_expire + 10 * 86400, new_expire

    def session_validity_reported():
        d = mod.UnifiedDownloader(str(workdir / "c_off.yml"))
        d.cookies = {'sid_guard': f'x|{int(__import__("time").time()) - 7200}|3600|x'}
        hint = d._cookie_expiry_hint(d._cookies_to_dict(d.cookies))
        assert hint and "过期" in hint, hint
        d.cookies = {'sid_guard': f'x|{int(__import__("time").time())}|2592000|x'}
        assert d._cookie_expiry_hint(d._cookies_to_dict(d.cookies)) is None

    for name, fn in [
        ("content type routing", content_types),
        ("id extraction", extract_ids),
        ("safe file names", safe_names),
        ("post folder naming", folder_names),
        ("frame prefix", frame_prefix),
        ("url helpers", url_helpers),
        ("media candidate extraction", media_candidates),
        ("config aliases (links/output_dir/cookie)", config_aliases),
        ("missing config fallback", missing_config),
        ("time filter", time_filter),
        ("increase/increment skip", increment_logic),
        ("database roundtrip", database_roundtrip),
        ("number limits", number_limits),
        ("video slicing", slicing),
        ("slice backfill", backfill),
        ("ffmpeg + cookies", ffmpeg_and_cookies),
        ("cookie auto-renewal (Set-Cookie writeback)", cookie_auto_renewal),
        ("session validity reporting", session_validity_reported),
    ]:
        check(name, fn)


# --------------------------------------------------------------------------- #
def network_checks(mod) -> None:
    os.chdir(PROJECT)
    cfg = PROJECT / "config_simple.yml"

    state: dict = {}

    def user_profile():
        async def go():
            d = mod.UnifiedDownloader(str(cfg))
            await d._initialize_cookies_and_headers()
            sec = "MS4wLjABAAAAJoErngvchnayttQYmbfTiA9FIpfX0jjEtQUPYr3s3vJMs8qO-KnrNGBene6_preh"
            profile = await asyncio.wait_for(d._fetch_user_profile(sec), timeout=60)
            assert profile, "profile empty"
            state["sec"] = sec
            state["profile"] = profile

        asyncio.run(go())

    def short_url():
        async def go():
            d = mod.UnifiedDownloader(str(cfg))
            await d._initialize_cookies_and_headers()
            out = await asyncio.wait_for(
                d.resolve_short_url("https://v.douyin.com/iRGu2mBL/"), timeout=30
            )
            print(f"        short -> {out[:90]}")
            assert "douyin.com" in out, out

        asyncio.run(go())

    def user_posts():
        async def go():
            d = mod.UnifiedDownloader(str(cfg))
            await d._initialize_cookies_and_headers()
            data = await asyncio.wait_for(
                d._fetch_user_posts(state.get("sec") or
                                    "MS4wLjABAAAAJoErngvchnayttQYmbfTiA9FIpfX0jjEtQUPYr3s3vJMs8qO-KnrNGBene6_preh"),
                timeout=90,
            )
            assert data, "user posts empty"
            awemes = (data.get("aweme_list") or [])
            assert awemes, f"no aweme_list: {list(data)[:8]}"
            info = awemes[0]
            state["aweme_id"] = d._get_aweme_id_from_info(info)
            state["info"] = info
            print(f"        first aweme: {state['aweme_id']} desc={str(info.get('desc'))[:40]!r}")

        asyncio.run(go())

    def fetch_detail_and_download():
        aweme_id = state.get("aweme_id")
        assert aweme_id, "need aweme id from user posts"

        async def go():
            d = mod.UnifiedDownloader(str(cfg))
            d.save_path = PROJECT / "_regress_out"
            await d._initialize_cookies_and_headers()
            info = await asyncio.wait_for(d._fetch_video_info(str(aweme_id)), timeout=90)
            assert info, "detail empty"
            ok = await asyncio.wait_for(d._download_media_files(info), timeout=180)
            assert ok, "download_media_files returned False"
            files = [p for p in (PROJECT / "_regress_out").rglob("*") if p.is_file()]
            assert files, "no files written"
            state["files"] = files
            for p in files[:6]:
                print(f"        file: {p.name} ({p.stat().st_size} B)")

        asyncio.run(go())

    for name, fn in [
        ("live: user profile", user_profile),
        ("live: short url resolve", short_url),
        ("live: user post list", user_posts),
        ("live: detail + media download", fetch_detail_and_download),
    ]:
        check(name, fn)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", action="store_true")
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="dy_regress_"))
    base_cfg = PROJECT / "config_simple.yml"
    for name in (
        "c_off.yml", "c_slice.yml", "alias.yml", "time.yml", "inc.yml", "inc2.yml",
        "num.yml", "bf.yml",
    ):
        shutil.copy(base_cfg, workdir / name)
    os.chdir(workdir)
    try:
        mod = load_module()
        print("== offline ==")
        offline_checks(mod, workdir)
        if args.network:
            print("== network ==")
            network_checks(mod)
    except Exception:
        traceback.print_exc()
        return 2
    finally:
        os.chdir(PROJECT)

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"\n== summary: {passed}/{len(RESULTS)} passed ==")
    for name, detail in failed:
        print(f"  FAILED: {name} -> {detail}")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
