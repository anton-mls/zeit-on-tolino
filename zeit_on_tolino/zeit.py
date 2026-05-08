import glob
import logging
import os
import time
from pathlib import Path
from typing import Tuple

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from zeit_on_tolino.env_vars import EnvVars, MissingEnvironmentVariable
from zeit_on_tolino.web import Delay

ZEIT_LOGIN_URL = "https://epaper.zeit.de/abo/diezeit"
ZEIT_DATE_FORMAT = "%d.%m.%Y"

BUTTON_TEXT_TO_RECENT_EDITION = "ZUR AKTUELLEN AUSGABE"
BUTTON_TEXT_DOWNLOAD_EPUB = "EPUB FÜR E-READER LADEN"
BUTTON_TEXT_EPUB_DOWNLOAD_IS_PENDING = "EPUB FOLGT IN KÜRZE"

LOGIN_TIMEOUT = 30

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _get_credentials() -> Tuple[str, str]:
    try:
        username = os.environ[EnvVars.ZEIT_PREMIUM_USER]
        password = os.environ[EnvVars.ZEIT_PREMIUM_PASSWORD]
        return username, password
    except KeyError:
        raise MissingEnvironmentVariable(
            f"Ensure to export your ZEIT username and password as environment variables "
            f"'{EnvVars.ZEIT_PREMIUM_USER}' and '{EnvVars.ZEIT_PREMIUM_PASSWORD}'. For "
            "Github Actions, use repository secrets."
        )


# ---------------------------------------------------------------------------
# Debugging Helpers
# ---------------------------------------------------------------------------


def dump_debug_artifacts(webdriver: WebDriver, name: str = "debug") -> None:
    """Save screenshot + HTML for easier debugging in GitHub Actions."""

    try:
        webdriver.save_screenshot(f"{name}.png")
    except Exception:
        pass

    try:
        with open(f"{name}.html", "w", encoding="utf-8") as f:
            f.write(webdriver.page_source)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cookie Handling
# ---------------------------------------------------------------------------


def _accept_cookies(webdriver: WebDriver) -> None:
    """Accept cookie banners if present."""

    cookie_selectors = [
        (By.XPATH, "//button[contains(., 'Akzeptieren')]"),
        (By.XPATH, "//button[contains(., 'Accept')]"),
        (By.XPATH, "//button[contains(., 'Zustimmen')]"),
        (By.CSS_SELECTOR, "button[data-testid='uc-accept-all-button']"),
    ]

    for selector in cookie_selectors:
        try:
            button = WebDriverWait(webdriver, 5).until(
                EC.element_to_be_clickable(selector)
            )

            log.info("Cookie dialog detected. Accepting cookies.")
            button.click()
            time.sleep(2)
            return

        except Exception:
            continue


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def _login(webdriver: WebDriver) -> None:
    username, password = _get_credentials()

    log.info("Opening ZEIT login page...")
    webdriver.get(ZEIT_LOGIN_URL)

    _accept_cookies(webdriver)

    wait = WebDriverWait(webdriver, LOGIN_TIMEOUT)

    try:
        log.info("Waiting for email field...")

        email_input = wait.until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "input[type='email'], input[name='email']",
                )
            )
        )

        email_input.clear()
        email_input.send_keys(username)

        log.info("Waiting for password field...")

        password_input = wait.until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "input[type='password']",
                )
            )
        )

        password_input.clear()
        password_input.send_keys(password)

        log.info("Waiting for submit button...")

        login_button = wait.until(
            EC.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    "button[type='submit'], input[type='submit']",
                )
            )
        )

        login_button.click()

    except TimeoutException as exc:
        dump_debug_artifacts(webdriver, "zeit_login_failure")

        raise RuntimeError(
            "Could not find ZEIT login form. The website layout probably changed."
        ) from exc

    log.info("Waiting for successful login...")

    try:
        wait.until(lambda d: "anmelden" not in d.current_url.lower())

    except TimeoutException as exc:
        dump_debug_artifacts(webdriver, "zeit_login_timeout")

        raise RuntimeError(
            "Failed to login. Credentials may be wrong or ZEIT changed the login flow."
        ) from exc

    log.info("Successfully logged into ZEIT.")


# ---------------------------------------------------------------------------
# Download Helpers
# ---------------------------------------------------------------------------


def _get_latest_downloaded_file_path(download_dir: str) -> Path:
    download_dir_files = glob.glob(f"{download_dir}/*")

    if not download_dir_files:
        raise RuntimeError("Download directory is empty.")

    latest_file = max(download_dir_files, key=os.path.getctime)
    return Path(latest_file)



def wait_for_downloads(path: str) -> None:
    time.sleep(Delay.small)

    start = time.time()

    while any(
        [
            filename.endswith(".crdownload")
            or filename.endswith(".part")
            for filename in os.listdir(path)
        ]
    ):
        now = time.time()

        if now > start + Delay.large:
            raise TimeoutError(
                f"Did not manage to download file within {Delay.large} seconds."
            )

        log.info("Waiting for download to finish...")
        time.sleep(2)


# ---------------------------------------------------------------------------
# Main EPUB Download Flow
# ---------------------------------------------------------------------------


def download_e_paper(webdriver: WebDriver) -> str:
    _login(webdriver)

    wait = WebDriverWait(webdriver, LOGIN_TIMEOUT)

    log.info("Looking for latest edition button...")

    try:
        latest_edition_button = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    f"//a[contains(., '{BUTTON_TEXT_TO_RECENT_EDITION}')]",
                )
            )
        )

        latest_edition_button.click()

    except TimeoutException as exc:
        dump_debug_artifacts(webdriver, "zeit_latest_edition_failure")

        raise RuntimeError(
            "Could not locate latest ZEIT edition button."
        ) from exc

    if BUTTON_TEXT_EPUB_DOWNLOAD_IS_PENDING in webdriver.page_source:
        raise RuntimeError(
            "New ZEIT release is available, however EPUB version is not yet published. Retry later."
        )

    log.info("Looking for EPUB download button...")

    try:
        download_button = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    f"//a[contains(., '{BUTTON_TEXT_DOWNLOAD_EPUB}')]",
                )
            )
        )

        log.info("Clicking EPUB download button...")
        download_button.click()

    except TimeoutException as exc:
        dump_debug_artifacts(webdriver, "zeit_epub_button_failure")

        raise RuntimeError(
            "Could not locate EPUB download button."
        ) from exc

    wait_for_downloads(webdriver.download_dir_path)

    e_paper_path = _get_latest_downloaded_file_path(
        webdriver.download_dir_path
    )

    if not e_paper_path.is_file():
        raise RuntimeError(
            "EPUB download failed. File does not exist after download completed."
        )

    log.info(f"Successfully downloaded EPUB: {e_paper_path}")

    return str(e_paper_path)
