import configparser
import datetime
import logging
import os
import re
import smtplib
import sys
from email.message import EmailMessage

import arxiv
import tenacity
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("arxiv_digest")


class Config:

    def __init__(self, settings_path="settings.ini", state_path=".last_check"):
        self._state_path = state_path
        self._read_settings(settings_path)
        self.last_check = self._read_state()

    def _read_settings(self, path):
        if not os.path.exists(path):
            log.error(f'"{path}" not found')
            sys.exit(1)

        config = configparser.ConfigParser()
        config.read(path)

        try:
            # arxiv
            self.subjects = config.get("arxiv", "subjects").lower().split()
            self.max_results = config.getint("arxiv", "max_results")
            self.page_size = config.getint("arxiv", "page_size", fallback=100)
            self.delay_seconds = config.getfloat("arxiv", "delay_seconds", fallback=5)
            self.num_retries = config.getint("arxiv", "num_retries", fallback=10)
            self.retry_attempts = config.getint("arxiv", "retry_attempts", fallback=2)
            self.retry_wait = config.getfloat("arxiv", "retry_wait", fallback=300)
            raw_keywords = config.get("arxiv", "keywords").strip()
            self.keywords = [k.strip() for k in raw_keywords.split(",") if k.strip()]

            # display
            self.font_face = config.get("display", "font_face")
            self.font_size = config.get("display", "font_size")
            self.show_in_console = config.getboolean("display", "show_in_console")

            # email
            self.email_enabled = config.getboolean("email", "enabled", fallback=False)
            if self.email_enabled:
                self.smtp_host = config.get("email", "smtp_host")
                self.smtp_port = config.getint("email", "smtp_port")
                self.email_from = config.get("email", "from")
                self.email_to = config.get("email", "to")
                self.smtp_user = os.getenv("SMTP_USER", "")
                self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        except (configparser.Error, ValueError) as e:
            log.error(f"Invalid settings: {e}")
            sys.exit(1)

    def _read_state(self):
        try:
            with open(self._state_path) as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return 0

    def save_state(self, timestamp):
        try:
            with open(self._state_path, "w") as f:
                f.write(str(timestamp))
        except OSError as e:
            log.warning(f"Could not save state: {e}")


class ArxivFetcher:

    def __init__(self, config):
        self.config = config

    def fetch(self):
        cutoff = (
            self.config.last_check
            or (
                datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=1)
            ).timestamp()
        )
        cutoff_dt = datetime.datetime.fromtimestamp(cutoff, tz=datetime.timezone.utc)
        date_title = f'since {cutoff_dt.strftime("%d %B %Y %H:%M UTC")}'
        date_name = str(datetime.datetime.now(tz=datetime.timezone.utc).date())

        query = "cat:" + " OR cat:".join(self.config.subjects)
        client = arxiv.Client(
            page_size=self.config.page_size,
            delay_seconds=self.config.delay_seconds,
            num_retries=self.config.num_retries,
        )
        search = arxiv.Search(
            query=query,
            max_results=self.config.max_results,
            sort_by=arxiv.SortCriterion.LastUpdatedDate,
        )

        retry = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(self.config.retry_attempts),
            wait=tenacity.wait_fixed(self.config.retry_wait),
            before_sleep=tenacity.before_sleep_log(log, logging.WARNING),
            reraise=True,
        )
        try:
            results = retry(lambda: list(client.results(search)))
        except Exception as e:
            log.error(f"Failed to fetch from arXiv: {e}")
            sys.exit(1)

        papers = self._filter_results(results, cutoff)
        papers.sort(key=lambda p: self.config.subjects.index(p["primary_category"]))
        return papers, date_title, date_name

    def _filter_results(self, results, cutoff):
        subjects = self.config.subjects
        papers = []

        for r in results:
            primary = r.primary_category.lower()
            if primary not in subjects:
                continue
            if r.updated.timestamp() <= cutoff:
                continue

            papers.append(
                {
                    "title": r.title,
                    "authors": [a.name for a in r.authors],
                    "categories": r.categories,
                    "primary_category": primary,
                    "pdf_url": r.pdf_url,
                    "updated": r.updated,
                }
            )

        return papers


class HtmlReport:

    def __init__(self, config):
        self.config = config

    def generate(self, papers, date_title):
        subject_labels = ", ".join(self._format_subject(s) for s in self.config.subjects)
        lines = [
            "<!DOCTYPE html>",
            "<html>",
            "<body>",
            f'<div style="font-family: {self.config.font_face}; font-size: {self.config.font_size};">',
            f"<p>{len(papers)} arXiv submissions from {date_title} from {subject_labels}:</p>",
        ]

        for paper in papers:
            title = self._highlight_keywords(" ".join(paper["title"].split()))
            authors = self._highlight_keywords(", ".join(paper["authors"]))
            tags = ", ".join(paper["categories"])
            lines += [
                "<br>",
                f'<a href="{paper["pdf_url"]}"><b>{title}</b></a>',
                f"[{tags}]",
                f"<br>{authors}",
                "<br>",
            ]

        lines += ["</div>", "</body>", "</html>"]
        return "\n".join(lines)

    def save(self, html, date_name):
        os.makedirs("html_files", exist_ok=True)
        path = f"html_files/arXiv submissions from {date_name}.html"
        with open(path, "w") as f:
            f.write(html)
        return path

    def _highlight_keywords(self, text):
        for keyword in self.config.keywords:
            pattern = re.compile(re.escape(keyword), re.IGNORECASE)
            text = pattern.sub(lambda m: f"<b><i>{m.group()}</i></b>", text)
        return text

    def _format_subject(self, subject):
        prefix, dot, suffix = subject.partition(".")
        return prefix + dot + suffix.upper()


def send_email(config, subject, html_body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.email_from
    msg["To"] = config.email_to
    msg.set_content("See HTML version.")
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
            server.starttls()
            server.login(config.smtp_user, config.smtp_password)
            server.send_message(msg)
        log.info("Email sent successfully")
    except Exception as e:
        log.error(f"Failed to send email: {e}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler("./arxiv_digest.log"), logging.StreamHandler()],
        format="%(asctime)s %(levelname)s %(module)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = Config()
    fetcher = ArxivFetcher(config)
    papers, date_title, date_name = fetcher.fetch()

    if not papers:
        log.info(f"No relevant arXiv submissions from {date_title}.")
        return

    # update state with the most recent paper's timestamp
    newest_ts = int(max(p["updated"].timestamp() for p in papers))
    config.save_state(newest_ts)

    # generate and save html report
    report = HtmlReport(config)
    html = report.generate(papers, date_title)
    filepath = report.save(html, date_name)
    log.info(f"Saved report to {filepath}")

    if config.show_in_console:
        for paper in papers:
            print(f'Title:    {paper["title"]}')
            print(f'Authors:  {", ".join(paper["authors"])}')
            print(f'Subjects: {", ".join(paper["categories"])}')
            print()

    if config.email_enabled:
        send_email(config, f"arXiv submissions from {date_name}", html)

    log.info(f"{len(papers)} arXiv submissions from {date_title}.")


if __name__ == "__main__":
    main()
