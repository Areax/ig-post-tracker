"""One-off diagnostic: does GitHub Actions' environment support full
client-side rendering/hydration at all, or only simple API fetch() calls?

Every one of the 7 "healthy" tracked handles has only ever been proven to
succeed via the primary web_profile_info fetch() path in CI - none of
them have ever needed the DOM-rendering fallbacks (extract_grid_timeline,
extract_embedded_timeline), since their primary call always succeeds.
Only bry.trieu (whose primary call is permanently broken on Instagram's
own side) has ever exercised those fallbacks from CI, and they fail
there every time despite succeeding locally.

This script forces a *healthy* handle through the DOM-rendering
fallbacks too, bypassing the primary call entirely, to distinguish two
hypotheses:
  A) GitHub's environment has a general limitation fully rendering/
     hydrating Instagram's client-side app - in which case this healthy
     handle's DOM-based extraction fails here too.
  B) The issue is specific to how Instagram serves bry.trieu's already-
     broken account from this environment - in which case a healthy
     handle's DOM-based extraction succeeds here.

Writes nothing to the DB or the live site - read-only, throwaway.
"""
import sys

from playwright.sync_api import sync_playwright

import check_posts as cp

HANDLE = sys.argv[1] if len(sys.argv) > 1 else "torch_boy"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cp.HEADLESS)
        context = browser.new_context(user_agent=cp.DESKTOP_UA, viewport={"width": 1280, "height": 800})
        page = context.new_page()

        failed_requests = []
        page.on("requestfailed", lambda req: failed_requests.append(
            f"{req.method} {req.url} -> {req.failure}"
        ))
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        response_summary = []
        page.on("response", lambda res: response_summary.append((res.url, res.status)))

        nav_error = cp.goto_profile(page, HANDLE)
        print(f"navigation error: {nav_error}")

        print(f"\n--- raw DOM probe ---")
        grid_link_count = page.eval_on_selector_all("a[href*='/p/'], a[href*='/reel/']", "els => els.length")
        img_count = page.eval_on_selector_all("img", "els => els.length")
        img_with_alt = page.eval_on_selector_all("img[alt]", "els => els.filter(e => e.alt && e.alt.length > 0).length")
        print(f"grid <a> links found: {grid_link_count}")
        print(f"<img> tags found: {img_count}")
        print(f"<img> tags with non-empty alt: {img_with_alt}")

        print(f"\n--- failed requests ({len(failed_requests)}) ---")
        for f in failed_requests[:30]:
            print(f"  {f}")

        print(f"\n--- console errors ({len(console_errors)}) ---")
        for c in console_errors[:20]:
            print(f"  {c[:200]}")

        non_200 = [r for r in response_summary if r[1] != 200]
        print(f"\n--- non-200 responses ({len(non_200)} of {len(response_summary)} total) ---")
        for url, status in non_200[:30]:
            print(f"  {status} {url}")

        print(f"\n--- extract_grid_timeline({HANDLE}) ---")
        grid_user_id, grid_media = cp.extract_grid_timeline(page)
        print(f"user_id: {grid_user_id}")
        print(f"media count: {len(grid_media)}")

        print(f"\n--- extract_embedded_timeline({HANDLE}) ---")
        embedded_user_id, embedded_media = cp.extract_embedded_timeline(page)
        print(f"user_id: {embedded_user_id}")
        print(f"media count: {len(embedded_media)}")

        verdict = "SUCCEEDED" if (grid_user_id or embedded_user_id) else "FAILED"
        print(f"\n=== VERDICT: DOM-based rendering extraction {verdict} for healthy handle {HANDLE} in this environment ===")

        browser.close()


if __name__ == "__main__":
    main()
