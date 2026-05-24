#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone


DEFAULT_INPUT_PATH = "/app/logs/gmail_bodies.jsonl"
DEFAULT_OUTPUT_PATH = "/app/logs/parsed_tickets.jsonl"
DEFAULT_IDS_PATH = "/app/logs/parsed_ids.json"
DEFAULT_WATCH_INTERVAL = 60

LSO_KEYWORDS = (
    "vandalism",
    "power failure",
    "no energy",
    "short circuit",
    "fiber",
)


class TicketParser:
    def __init__(self, input_path, output_path, ids_path):
        self.input_path = input_path
        self.output_path = output_path
        self.ids_path = ids_path

    def process(self):
        processed_ids = self.load_processed_ids()
        new_ids = set()
        parsed_count = 0
        skipped_count = 0

        if not os.path.exists(self.input_path):
            logging.info("Input file does not exist yet: %s", self.input_path)
            return 0

        self.ensure_parent_dir(self.output_path)
        self.ensure_parent_dir(self.ids_path)

        with open(self.input_path, "r", encoding="utf-8") as input_file, open(
            self.output_path, "a", encoding="utf-8"
        ) as output_file:
            for line_number, line in enumerate(input_file, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    logging.error(
                        "Bad JSON in %s line %s: %s",
                        self.input_path,
                        line_number,
                        exc,
                    )
                    continue

                first_msg_id = entry.get("first_msg_id")
                if first_msg_id and first_msg_id in processed_ids:
                    skipped_count += 1
                    continue

                try:
                    record = self.parse_entry(entry)
                    output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    output_file.flush()
                    parsed_count += 1
                    if first_msg_id:
                        new_ids.add(first_msg_id)
                except Exception:
                    logging.exception(
                        "Unexpected parser error in %s line %s; skipping entry",
                        self.input_path,
                        line_number,
                    )

        if new_ids:
            processed_ids.update(new_ids)
            self.save_processed_ids(processed_ids)

        logging.info(
            "Parsed %s new entries; skipped %s already processed entries",
            parsed_count,
            skipped_count,
        )
        return parsed_count

    def parse_entry(self, entry):
        warnings = []
        subject = self.as_text(entry.get("subject"))
        body = self.as_text(entry.get("body"))

        subject_fields = self.parse_subject(subject, warnings)
        body_fields = self.parse_body(body, warnings)

        record = {
            "wot": subject_fields.get("wot"),
            "customer_ticket": body_fields.get("customer_ticket"),
            "site_id": subject_fields.get("site_id"),
            "sector": subject_fields.get("sector"),
            "failure_type": subject_fields.get("failure_type"),
            "technologies": subject_fields.get("technologies"),
            "priority": subject_fields.get("priority"),
            "location_address": body_fields.get("location_address"),
            "location_lat": body_fields.get("location_lat"),
            "location_lon": body_fields.get("location_lon"),
            "thread_id": entry.get("thread_id"),
            "first_msg_id": entry.get("first_msg_id"),
            "body_snippet": body[:300],
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "sender": entry.get("from"),
            "site_full": subject_fields.get("site_full"),
            "body_has_lso_keywords": self.body_has_lso_keywords(body),
            "total_msgs_in_thread": entry.get("total_msgs_in_thread"),
        }

        self.add_input_warnings(entry, warnings)

        if warnings:
            record["parse_warnings"] = warnings

        return record

    def parse_subject(self, subject, warnings):
        fields = {
            "wot": None,
            "priority": None,
            "site_id": None,
            "site_full": None,
            "sector": None,
            "technologies": None,
            "failure_type": None,
        }

        match = re.search(r"WOT\d+", subject)
        if match:
            fields["wot"] = match.group(0)
        else:
            warnings.append("missing wot in subject")

        match = re.search(r"\*\*(P\d+)\*\*", subject)
        if match:
            fields["priority"] = match.group(1)

        match = re.search(r"([A-Z]{2}\d+[A-Z]*)_([A-Z0-9]+)", subject)
        if match:
            fields["site_id"] = match.group(1)
            fields["site_full"] = "%s_%s" % (match.group(1), match.group(2))
            fields["sector"] = match.group(2)
        else:
            warnings.append("missing site code in subject")

        match = re.search(r"Sector\s+(\d+)", subject, re.IGNORECASE)
        if match:
            fields["sector"] = match.group(1)
        elif not fields["sector"]:
            warnings.append("missing sector in subject")

        match = re.search(r"(?:N2500/)?L1900/L2100", subject)
        if match:
            fields["technologies"] = match.group(0)
        else:
            warnings.append("missing technologies in subject")

        match = re.search(r"(?:Down|Flapping)(?:\s*[-\u2013\u2014]\s*(.+))", subject)
        if not match:
            match = re.search(r"(?:Down|Flapping)\s+with\s+(.+)", subject, re.IGNORECASE)
        if match:
            fields["failure_type"] = self.clean_failure_type(match.group(1))
        else:
            warnings.append("missing failure type in subject")

        return fields

    def parse_body(self, body, warnings):
        fields = {
            "customer_ticket": None,
            "location_address": None,
            "location_lat": None,
            "location_lon": None,
        }

        match = re.search(r"Customer ticket:\s*(\S+)", body, re.IGNORECASE)
        if match:
            fields["customer_ticket"] = match.group(1)
        else:
            warnings.append("missing customer ticket in body")

        match = re.search(
            r"Location\s*\(Lat/Long\):\s*(.+?)[\r\n]+\s*\(([\d.-]+)/([\d.-]+)\)",
            body,
            re.IGNORECASE,
        )
        if match:
            fields["location_address"] = match.group(1).strip()
            fields["location_lat"] = match.group(2)
            fields["location_lon"] = match.group(3)
        else:
            warnings.append("missing location or coordinates in body")

        return fields

    def add_input_warnings(self, entry, warnings):
        for field_name in ("thread_id", "first_msg_id", "from", "subject", "body"):
            if entry.get(field_name) in (None, ""):
                warnings.append("missing input field: %s" % field_name)

        if "total_msgs_in_thread" not in entry:
            warnings.append("missing input field: total_msgs_in_thread")

    def body_has_lso_keywords(self, body):
        lower_body = body.lower()
        return any(keyword in lower_body for keyword in LSO_KEYWORDS)

    def clean_failure_type(self, value):
        value = value.strip()
        value = re.split(r"\s+(?:WOT\d+|\*\*P\d+\*\*)", value, maxsplit=1)[0]
        return value.strip() or None

    def load_processed_ids(self):
        if not os.path.exists(self.ids_path):
            return set()

        try:
            with open(self.ids_path, "r", encoding="utf-8") as ids_file:
                data = json.load(ids_file)
        except (OSError, json.JSONDecodeError) as exc:
            logging.error("Could not load processed IDs from %s: %s", self.ids_path, exc)
            return set()

        if not isinstance(data, list):
            logging.error("Processed IDs file is not a JSON array: %s", self.ids_path)
            return set()

        return set(str(item) for item in data if item is not None)

    def save_processed_ids(self, processed_ids):
        temp_path = self.ids_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as ids_file:
            json.dump(sorted(processed_ids), ids_file, indent=2)
            ids_file.write("\n")
        os.replace(temp_path, self.ids_path)

    def ensure_parent_dir(self, path):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def as_text(self, value):
        if value is None:
            return ""
        return str(value)


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Parse on-call dispatch tickets.")
    parser.add_argument("--watch", action="store_true", help="watch for new entries")
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH, help="input JSONL path")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="output JSONL path")
    parser.add_argument("--ids", default=DEFAULT_IDS_PATH, help="processed IDs JSON path")
    parser.add_argument(
        "--interval",
        default=DEFAULT_WATCH_INTERVAL,
        type=int,
        help="watch interval in seconds",
    )
    return parser.parse_args(argv)


def main(argv=None):
    configure_logging()
    args = parse_args(argv)
    ticket_parser = TicketParser(args.input, args.output, args.ids)

    if args.watch:
        logging.info("Watching %s every %s seconds", args.input, args.interval)
        while True:
            try:
                ticket_parser.process()
            except Exception:
                logging.exception("Unexpected top-level error during watch cycle")
            time.sleep(args.interval)

    try:
        ticket_parser.process()
    except Exception:
        logging.exception("Unexpected top-level error")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
