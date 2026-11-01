"""Tests of the real page in a real browser. Skipped unless Playwright is installed.
Set BROWSER_CHANNEL=chrome to use an installed Chrome instead of a downloaded Chromium."""
import os
import threading
import time
import unittest

from tests.support import URF, Case, pdf, png

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

WIDTHS = (320, 360, 375, 390, 430, 768, 1280)
LONG_NAME = "A very long WhatsApp document name that goes on and on 2026-11-01 final copy.pdf"
UNBROKEN = "Averylongfilenamewithoutanyspacesatallthatmustwraponaphonescreen" * 2 + ".pdf"


@unittest.skipUnless(sync_playwright, "Playwright not installed")
class BrowserCase(Case):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel=os.environ.get("BROWSER_CHANNEL") or None)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        super().setUp()
        self.running = True
        self.poller = threading.Thread(target=self.keep_polling, daemon=True)
        self.poller.start()
        self.addCleanup(self.stop_polling)
        self.errors = []
        self.open_page()

    def keep_polling(self):
        while self.running:
            self.poll()
            time.sleep(0.1)

    def stop_polling(self):
        self.running = False
        self.poller.join()

    def open_page(self, **options):
        options.setdefault("viewport", {"width": 390, "height": 844})
        self.context = self.browser.new_context(**options)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        self.page.goto(self.base)
        expect(self.page.locator("#hero b")).not_to_have_text("Loading…")

    def tearDown(self):
        self.assertEqual(self.errors, [], "browser errors")

    def choose(self, *files):
        self.page.set_input_files("#file", [{"name": n, "mimeType": m, "buffer": d} for n, m, d in files])

    def wait_for(self, check, seconds=5):
        end = time.time() + seconds
        while time.time() < end:
            if check():
                return
            time.sleep(0.05)
        self.fail("condition not met in time")


class LayoutTests(BrowserCase):
    def fill(self):
        self.fake.add_job(LONG_NAME, state=5, data=URF)
        self.fake.add_job(UNBROKEN, state=3)
        self.fake.add_job(UNBROKEN, state=9)
        self.fake.notice = "A long printer notice " * 8
        self.settle()

    def overflow(self):
        return self.page.evaluate("""() => {
            const width = document.documentElement.clientWidth;
            return {page: document.documentElement.scrollWidth - width,
                    parts: [...document.querySelectorAll('main *')].filter(e => e.offsetParent && e.getBoundingClientRect().right > width + 1).map(e => e.id || e.className || e.tagName)};
        }""")

    def test_no_sideways_scroll_at_any_width(self):
        self.fill()
        for width in WIDTHS:
            self.page.set_viewport_size({"width": width, "height": 800})
            self.page.reload()
            expect(self.page.locator("#jobs .job").first).to_be_visible()
            self.choose((LONG_NAME, "application/pdf", pdf()), (UNBROKEN, "application/pdf", pdf()))
            self.assertEqual(self.overflow(), {"page": 0, "parts": []}, width)

    def test_controls_are_big_enough_to_tap(self):
        self.fill()
        self.choose(("a.pdf", "application/pdf", pdf()))
        small = self.page.evaluate("""() => [...document.querySelectorAll('button:not(.switch), .pick, label.field')]
            .filter(e => e.offsetParent).map(e => e.getBoundingClientRect())
            .filter(r => r.height < 44 || r.width < 44).length""")
        self.assertEqual(small, 0)

    def test_pinch_zoom_stays_allowed(self):
        self.assertNotIn("user-scalable=no", self.page.locator("meta[name=viewport]").get_attribute("content"))

    def test_phone_with_touch(self):
        self.open_page(**self.playwright.devices["iPhone 13"])
        self.choose(("a.pdf", "application/pdf", pdf()))
        self.page.locator("#more").tap()
        self.page.locator("#send").tap()
        expect(self.page.locator("#result")).to_contain_text("Sent")
        self.assertEqual(self.last_job()["copies"], 2)

    def test_dark_mode(self):
        self.open_page(color_scheme="dark")
        background = self.page.evaluate("getComputedStyle(document.body).backgroundColor")
        self.assertEqual(background, "rgb(17, 19, 24)")

    def test_landscape_phone(self):
        self.fill()
        self.page.set_viewport_size({"width": 844, "height": 390})
        self.assertEqual(self.overflow(), {"page": 0, "parts": []})


class StatusTests(BrowserCase):
    def test_title_comes_from_settings(self):
        expect(self.page.locator("#title")).to_have_text("Test Printer")
        self.assertEqual(self.page.title(), "Test Printer")

    def test_ready(self):
        expect(self.page.locator("#hero b")).to_have_text("Ready")

    def test_printing_with_progress(self):
        self.fake.add_job(LONG_NAME, state=5, data=URF)
        self.settle()
        expect(self.page.locator("#hero b")).to_have_text("Printing")
        expect(self.page.locator("#jobs")).to_contain_text("Page 1 of 7")
        self.fake.sheets += 3
        expect(self.page.locator("#jobs")).to_contain_text("Page 4 of 7", timeout=10000)

    def test_out_of_paper(self):
        self.fake.printer_reasons = ["media-empty-error"]
        self.monitor.next_printer = 0
        expect(self.page.locator("#hero b")).to_have_text("Out of paper", timeout=8000)

    def test_printer_unreachable(self):
        self.monitor.printer.client.url = "http://127.0.0.1:9"
        self.monitor.printer.http_port = 9
        expect(self.page.locator("#hero b")).to_have_text("Printer not reachable", timeout=8000)

    def test_cups_login_problem_is_explained(self):
        self.monitor.cups.client.password = "wrong"
        expect(self.page.locator("#hero b")).to_have_text("CUPS login failed", timeout=8000)

    def test_dashboard_going_away(self):
        self.page.route("**/api/status", lambda route: route.abort())
        expect(self.page.locator("#hero b")).to_have_text("Dashboard offline", timeout=8000)
        self.page.unroute("**/api/status")
        expect(self.page.locator("#hero b")).to_have_text("Ready", timeout=8000)
        self.errors.clear()

    def test_other_printers_hide_what_they_cant_report(self):
        self.fake.hp = False
        self.monitor.next_printer = 0
        expect(self.page.locator("#stats .stat")).to_have_count(2, timeout=8000)
        expect(self.page.locator("#info")).to_have_text("Acme Inkjet 100")

    def test_note_is_shown(self):
        self.monitor.settings = self.make_settings(note="Paper is in the bottom tray")
        expect(self.page.locator("#info")).to_contain_text("Paper is in the bottom tray")

    def test_history_labels(self):
        self.fake.add_job("a.pdf", state=9, user="mobile")
        self.fake.add_job("b.pdf", state=9, user="dashboard")
        expect(self.page.locator("#history")).to_contain_text("iPhone")
        expect(self.page.locator("#history")).to_contain_text("Dashboard")

    def test_refresh_leaves_unchanged_parts_alone_so_taps_land(self):
        self.fake.add_job("a.pdf", state=3, data=URF)
        self.settle()
        button = self.page.locator("[data-cancel]")
        expect(button).to_be_visible()
        button.evaluate("e => { e.dataset.mark = 'same' }")
        self.page.wait_for_timeout(4000)
        self.assertEqual(button.get_attribute("data-mark"), "same")


class ScriptInjectionTests(BrowserCase):
    PAYLOADS = ('<img src=x onerror="window.hacked=1">', '"><script>window.hacked=1</script>', "'><svg onload=window.hacked=1>")

    def test_hostile_job_names(self):
        for payload in self.PAYLOADS:
            self.fake.add_job(payload, state=3, user=payload)
            self.fake.add_job(payload, state=9, user=payload)
        self.settle()
        expect(self.page.locator("#jobs .job")).to_have_count(3)
        self.page.wait_for_timeout(3500)
        self.assertIsNone(self.page.evaluate("window.hacked ?? null"))
        expect(self.page.locator("#jobs")).to_contain_text("<img src=x")

    def test_hostile_upload_name(self):
        self.choose((self.PAYLOADS[0], "application/pdf", pdf()))
        self.assertIsNone(self.page.evaluate("window.hacked ?? null"))
        expect(self.page.locator("#files")).to_contain_text("<img src=x")

    def test_hostile_printer_notice_and_model(self):
        self.fake.notice = "&lt;img src=x onerror=window.hacked=1&gt;"
        self.fake.model = self.PAYLOADS[0]
        self.monitor.next_printer = 0
        expect(self.page.locator("#notices")).to_contain_text("<img src=x", timeout=8000)
        self.assertIsNone(self.page.evaluate("window.hacked ?? null"))

    def test_policy_blocks_inline_script_even_if_escaping_failed(self):
        self.page.evaluate("""() => { const s = document.createElement('script'); s.textContent = 'window.hacked = 1'; document.body.append(s); }""")
        self.assertIsNone(self.page.evaluate("window.hacked ?? null"))
        self.errors.clear()


class UploadTests(BrowserCase):
    def test_picker_accepts_only_supported_types(self):
        self.assertEqual(self.page.locator("#file").get_attribute("accept"), "application/pdf,image/jpeg,image/png")

    def test_full_upload(self):
        self.choose(("report.pdf", "application/pdf", pdf()))
        expect(self.page.locator("#pick")).to_be_hidden()
        expect(self.page.locator("#files")).to_contain_text("report.pdf")
        self.page.click("#more")
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("You can close this page")
        expect(self.page.locator("#pick")).to_be_visible()
        job = self.last_job()
        self.assertEqual((job["name"], job["copies"], job["data"]), ("report.pdf", 2, pdf()))
        expect(self.page.locator("#jobs")).to_contain_text("report.pdf", timeout=8000)

    def test_several_files(self):
        self.choose(("a.pdf", "application/pdf", pdf()), ("b.png", "image/png", png()))
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("2 files sent")
        self.assertEqual([j["name"] for j in self.fake.jobs.values()], ["a.pdf", "b.png"])

    def test_clear(self):
        self.choose(("a.pdf", "application/pdf", pdf()))
        self.page.click("#clear")
        expect(self.page.locator("#options")).to_be_hidden()
        expect(self.page.locator("#pick")).to_be_visible()
        self.assertEqual(self.fake.jobs, {})

    def test_same_file_can_be_chosen_again(self):
        file = ("a.pdf", "application/pdf", pdf())
        self.choose(file)
        self.page.click("#clear")
        self.choose(file)
        expect(self.page.locator("#options")).to_be_visible()

    def test_copies_stay_in_range(self):
        self.choose(("a.pdf", "application/pdf", pdf()))
        expect(self.page.locator("#less")).to_be_disabled()
        for _ in range(19):
            self.page.click("#more")
        expect(self.page.locator("#copies")).to_have_text("20")
        expect(self.page.locator("#more")).to_be_disabled()

    def test_copies_reset_for_the_next_file(self):
        self.choose(("a.pdf", "application/pdf", pdf()))
        self.page.click("#more")
        self.page.click("#clear")
        self.choose(("b.pdf", "application/pdf", pdf()))
        expect(self.page.locator("#copies")).to_have_text("1")

    def test_colour_choice_hidden_when_toggle_off(self):
        self.page.click("#color")
        expect(self.page.locator("#color")).to_have_attribute("aria-checked", "false", timeout=8000)
        self.choose(("a.pdf", "application/pdf", pdf()))
        expect(self.page.locator("#colorField")).to_be_hidden()
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("Sent")
        self.assertEqual(self.last_job()["options"]["print-color-mode"], ["monochrome"])

    def test_colour_ticked_prints_in_colour(self):
        self.choose(("a.pdf", "application/pdf", pdf()))
        self.page.check("#useColor")
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("Sent")
        self.assertEqual(self.last_job()["options"]["print-color-mode"], ["color"])

    def test_colour_unticked_prints_in_black_and_white(self):
        self.choose(("a.pdf", "application/pdf", pdf()))
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("Sent")
        self.assertEqual(self.last_job()["options"]["print-color-mode"], ["monochrome"])

    def test_tick_is_dropped_if_toggle_goes_off(self):
        self.choose(("a.pdf", "application/pdf", pdf()))
        self.page.check("#useColor")
        self.fake.color = False
        expect(self.page.locator("#colorField")).to_be_hidden(timeout=8000)
        self.assertFalse(self.page.is_checked("#useColor"))

    def test_too_big_is_stopped_before_sending(self):
        self.page.route("**/api/status", lambda route: route.fulfill(json=self.small_limits()))
        self.page.reload()
        self.choose(("big.pdf", "application/pdf", pdf() + b"\0" * (2 * 1024 * 1024)))
        expect(self.page.locator("#result")).to_contain_text("big.pdf is over 1 MB")
        self.assertEqual(self.fake.jobs, {})

    def small_limits(self):
        status = self.monitor.status()
        status["limits"]["upload_mb"] = 1
        return status

    def test_unsupported_file_shows_the_reason(self):
        self.choose(("notes.pdf", "application/pdf", b"really just text"))
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("Only PDF, JPEG and PNG")
        expect(self.page.locator("#files")).to_contain_text("notes.pdf")
        self.errors.clear()

    def test_queue_refusing_shows_a_message_and_keeps_the_file(self):
        self.fake.accepting = False
        self.choose(("a.pdf", "application/pdf", pdf()))
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("didn't take the file")
        expect(self.page.locator("#send")).to_be_visible()
        self.errors.clear()

    def test_partial_failure_keeps_only_unsent_files(self):
        self.choose(("good.pdf", "application/pdf", pdf()), ("bad.pdf", "application/pdf", b"nope"), ("after.pdf", "application/pdf", pdf()))
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("1 sent")
        expect(self.page.locator("#result")).to_contain_text("bad.pdf")
        expect(self.page.locator("#files")).not_to_contain_text("good.pdf")
        expect(self.page.locator("#files")).to_contain_text("after.pdf")
        self.assertEqual([j["name"] for j in self.fake.jobs.values()], ["good.pdf"])
        self.errors.clear()

    def test_network_failure_shows_a_message(self):
        self.page.route("**/api/print*", lambda route: route.abort())
        self.choose(("a.pdf", "application/pdf", pdf()))
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("upload failed")
        self.errors.clear()

    def test_unicode_name(self):
        self.choose(("चालान रसीद.pdf", "application/pdf", pdf()))
        self.page.click("#send")
        expect(self.page.locator("#result")).to_contain_text("Sent")
        self.assertEqual(self.last_job()["name"], "चालान रसीद.pdf")


class ActionTests(BrowserCase):
    def test_cancel_confirmed(self):
        job_id = self.fake.add_job("a.pdf", state=5, data=URF)
        self.settle()
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.click("[data-cancel]")
        self.wait_for(lambda: self.fake.jobs[job_id]["state"] == 7)
        expect(self.page.locator("#jobs")).to_contain_text("Nothing printing", timeout=8000)

    def test_cancel_declined(self):
        job_id = self.fake.add_job("a.pdf", state=5, data=URF)
        self.settle()
        self.page.once("dialog", lambda dialog: dialog.dismiss())
        self.page.click("[data-cancel]")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.fake.jobs[job_id]["state"], 5)

    def test_toggle(self):
        self.page.click("#color")
        expect(self.page.locator("#color")).to_have_attribute("aria-checked", "false", timeout=8000)
        self.assertFalse(self.fake.color)
        expect(self.page.locator("#colorNote")).to_contain_text("black and white")
        expect(self.page.locator("#color")).to_be_enabled()
        self.page.click("#color")
        expect(self.page.locator("#color")).to_have_attribute("aria-checked", "true", timeout=8000)
        self.assertIn("*ColorModel RGB", self.fake.ppd)

    def test_toggle_locked_during_a_job(self):
        self.fake.add_job("a.pdf", state=5, data=URF)
        self.settle()
        expect(self.page.locator("#color")).to_be_disabled(timeout=8000)
        expect(self.page.locator("#colorNote")).to_contain_text("once printing finishes")


if __name__ == "__main__":
    unittest.main()
