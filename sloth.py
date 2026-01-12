#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import queue
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


DEFAULT_GROUPS_URL = (
    "https://backoffice.algoritmika.org/group"
    "?GroupSearch%5Bstatus%5D%5B0%5D=active"
    "&GroupSearch%5Bstatus%5D%5B1%5D=recruiting"
    "&presetType=all"
    "&page=1"
    "&_pjax=%23group-grid-pjax"
)

ACTIVITY_URL_TEMPLATE = (
    "https://backoffice.algoritmika.org/api/v2/group/activity/index"
    "?ownerId={owner_id}&page={page}&perPage={per_page}"
)

LESSON_VIEW_URL_TEMPLATE = (
    "https://backoffice.algoritmika.org/api/v2/group/lesson/view/{group_lesson_id}"
    "?expand=levels%2Cprogress"
)

POST_URL = "https://backoffice.algoritmika.org/platform/api/v1/teacherComment/step"

CHANGE_MULTI_STATUS_URL_TEMPLATE = (
    "https://backoffice.algoritmika.org/platform/api/v2/level/change-multi-status"
    "?groupId={groupId}&studentId={studentId}"
)

GROUP_IDS_FILE = "groups_id.txt"
GROUP_LESSON_IDS_FILE = "group_lesson_ids.txt"
OBJECTIVES_JSONL_FILE = "objectives_to_review.jsonl"
POST_RESULTS_JSONL_FILE = "post_results.jsonl"


def parse_cookie_header(cookie_header: str) -> Dict[str, str]:
    cookie_header = (cookie_header or "").strip()
    if not cookie_header:
        return {}
    c = SimpleCookie()
    c.load(cookie_header)
    return {k: morsel.value for k, morsel in c.items()}


def read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def read_ids(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    out: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                out.append(s)
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def write_ids(path: str, ids: List[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for x in ids:
            f.write(str(x) + "\n")


def safe_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def extract_group_ids_from_groups_html(html: str) -> List[str]:
    row_pattern = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    td_id_pattern = re.compile(r'<td\b[^>]*data-col-seq="id"[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)
    td_next_pattern = re.compile(r'<td\b[^>]*data-col-seq="nextLessonTime"[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)

    def clean_text(s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s)
        s = s.replace("&nbsp;", " ")
        return s.strip()

    ids: List[str] = []
    for row_html in row_pattern.findall(html):
        id_cell = td_id_pattern.search(row_html)
        next_cell = td_next_pattern.search(row_html)
        if not id_cell or not next_cell:
            continue
        gid = clean_text(id_cell.group(1))
        nxt = clean_text(next_cell.group(1))
        if nxt and nxt not in {"-", "—"} and gid:
            ids.append(gid)

    seen = set()
    uniq = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def collect_group_lesson_ids(obj: Any, out: Set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "groupLessonId" and v is not None:
                out.add(str(v))
            else:
                collect_group_lesson_ids(v, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_group_lesson_ids(item, out)


def extract_params_from_url(u: Optional[str]) -> Dict[str, Optional[int]]:
    if not u or not isinstance(u, str):
        return {"levelId": None, "student": None, "groupId": None, "lesson": None, "task": None, "level": None}
    parsed = urlparse(u)
    qs = parse_qs(parsed.query)

    def get_int(key: str) -> Optional[int]:
        v = qs.get(key)
        if not v:
            return None
        try:
            return int(v[0])
        except Exception:
            return None

    return {
        "levelId": get_int("levelId"),
        "student": get_int("student"),
        "groupId": get_int("groupId"),
        "lesson": get_int("lesson"),
        "task": get_int("task"),
        "level": get_int("level"),
    }


def extract_objectives_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return rows

    group_lesson_id = data.get("id")
    students = data.get("progress", [])
    if not isinstance(students, list):
        return rows

    for student_entry in students:
        if not isinstance(student_entry, dict):
            continue

        tasks = student_entry.get("progress", [])
        if not isinstance(tasks, list):
            continue

        for task_entry in tasks:
            if not isinstance(task_entry, dict):
                continue

            task_id_fallback = safe_int(task_entry.get("id"))
            task_position_fallback = safe_int(task_entry.get("position"))

            levels = task_entry.get("levels", [])
            if not isinstance(levels, list):
                continue

            for lvl in levels:
                if not isinstance(lvl, dict):
                    continue

                url = lvl.get("url")
                p = extract_params_from_url(url)

                level_id = p.get("levelId")
                student_id = p.get("student")
                group_id = p.get("groupId")
                lesson_id = p.get("lesson")
                task_id = p.get("task") or task_id_fallback

                if level_id is None or student_id is None:
                    continue

                position = safe_int(lvl.get("position")) or task_position_fallback

                objectives = lvl.get("objectives", [])
                if not isinstance(objectives, list):
                    continue

                for obj in objectives:
                    if not isinstance(obj, dict):
                        continue

                    hint_id = obj.get("id")
                    teacher_status = obj.get("teacherStatus")
                    student_status = obj.get("studentStatus")
                    if hint_id is None:
                        continue

                    if teacher_status == "success" and student_status == "checked":
                        rows.append({
                            "hint_id": str(hint_id),
                            "level": int(level_id),
                            "student": int(student_id),
                            "groupId": int(group_id) if group_id is not None else None,
                            "lessonId": int(lesson_id) if lesson_id is not None else None,
                            "taskId": int(task_id) if task_id is not None else None,
                            "position": int(position) if position is not None else None,
                            "groupLessonId": group_lesson_id,
                        })

    return rows


@dataclass
class ClientConfig:
    cookies: Dict[str, str]
    timeout: float = 30.0
    sleep: float = 0.2
    per_page: int = 50
    pages_per_group: int = 1


def make_session() -> requests.Session:
    return requests.Session()


def default_headers_json() -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "algoritmika-automation/1.0 (+requests)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://backoffice.algoritmika.org/",
        "Origin": "https://backoffice.algoritmika.org",
    }


def default_headers_html() -> Dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": "algoritmika-automation/1.0 (+requests)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://backoffice.algoritmika.org/group",
        "Origin": "https://backoffice.algoritmika.org",
    }


def step1_fetch_groups(groups_url: str, cfg: ClientConfig, log) -> List[str]:
    log(f"STEP 1: Fetch groups page\nURL: {groups_url}")
    session = make_session()
    r = session.get(groups_url, headers=default_headers_html(), cookies=cfg.cookies, timeout=cfg.timeout)
    log(f"HTTP {r.status_code}, Content-Type: {r.headers.get('Content-Type')}")
    if r.status_code in (401, 403):
        raise RuntimeError(f"Auth error on groups page (HTTP {r.status_code}).")
    if r.status_code != 200:
        raise RuntimeError(f"Unexpected HTTP {r.status_code} on groups page.")
    ids = extract_group_ids_from_groups_html(r.text)
    write_ids(GROUP_IDS_FILE, ids)
    log(f"Extracted group IDs with nextLessonTime: {len(ids)} -> {GROUP_IDS_FILE}")
    return ids


def step2_fetch_group_lesson_ids(group_ids: List[str], cfg: ClientConfig, log) -> List[str]:
    log(f"STEP 2: Fetch group activity for {len(group_ids)} groups -> collect groupLessonId")
    session = make_session()
    headers = default_headers_json()
    all_ids: Set[str] = set()

    for i, gid in enumerate(group_ids, start=1):
        for page in range(1, max(1, cfg.pages_per_group) + 1):
            url = ACTIVITY_URL_TEMPLATE.format(owner_id=gid, page=page, per_page=cfg.per_page)
            try:
                r = session.get(url, headers=headers, cookies=cfg.cookies, timeout=cfg.timeout)
            except requests.RequestException as e:
                log(f"[{i}/{len(group_ids)}] group={gid} page={page} request failed: {e}")
                time.sleep(cfg.sleep)
                continue

            if r.status_code in (401, 403):
                raise RuntimeError(f"Auth error on activity (HTTP {r.status_code}) for group {gid}.")
            if r.status_code != 200:
                log(f"[{i}/{len(group_ids)}] group={gid} page={page} HTTP {r.status_code} preview={r.text[:120]!r}")
                time.sleep(cfg.sleep)
                continue

            try:
                data = r.json()
            except ValueError:
                log(f"[{i}/{len(group_ids)}] group={gid} page={page} non-JSON preview={r.text[:120]!r}")
                time.sleep(cfg.sleep)
                continue

            before = len(all_ids)
            collect_group_lesson_ids(data, all_ids)
            added = len(all_ids) - before
            log(f"[{i}/{len(group_ids)}] group={gid} page={page}: +{added} groupLessonId")
            time.sleep(cfg.sleep)

    out = sorted(all_ids, key=lambda x: int(x) if x.isdigit() else x)
    write_ids(GROUP_LESSON_IDS_FILE, out)
    log(f"Total unique groupLessonId: {len(out)} -> {GROUP_LESSON_IDS_FILE}")
    return out


def step3_fetch_objectives(group_lesson_ids: List[str], cfg: ClientConfig, log) -> int:
    log(f"STEP 3: Fetch lesson view for {len(group_lesson_ids)} groupLessonId -> {OBJECTIVES_JSONL_FILE}")
    session = make_session()
    headers = default_headers_json()

    seen = set()
    written = 0
    errors = 0

    with open(OBJECTIVES_JSONL_FILE, "w", encoding="utf-8") as fout:
        for i, glid in enumerate(group_lesson_ids, start=1):
            url = LESSON_VIEW_URL_TEMPLATE.format(group_lesson_id=glid)

            try:
                r = session.get(url, headers=headers, cookies=cfg.cookies, timeout=cfg.timeout)
            except requests.RequestException as e:
                errors += 1
                log(f"[{i}/{len(group_lesson_ids)}] groupLessonId={glid} request failed: {e}")
                time.sleep(cfg.sleep)
                continue

            if r.status_code in (401, 403):
                raise RuntimeError(f"Auth error on lesson view (HTTP {r.status_code}) for groupLessonId {glid}.")
            if r.status_code != 200:
                errors += 1
                log(f"[{i}/{len(group_lesson_ids)}] groupLessonId={glid} HTTP {r.status_code} preview={r.text[:120]!r}")
                time.sleep(cfg.sleep)
                continue

            try:
                payload = r.json()
            except ValueError:
                errors += 1
                log(f"[{i}/{len(group_lesson_ids)}] groupLessonId={glid} non-JSON preview={r.text[:120]!r}")
                time.sleep(cfg.sleep)
                continue

            if payload.get("status") != "success":
                errors += 1
                log(f"[{i}/{len(group_lesson_ids)}] groupLessonId={glid} API status={payload.get('status')!r}")
                time.sleep(cfg.sleep)
                continue

            rows = extract_objectives_rows(payload)

            added = 0
            for row in rows:
                key = (row["hint_id"], row["level"], row["student"], row.get("groupLessonId"))
                if key in seen:
                    continue
                seen.add(key)
                fout.write(json.dumps(row, ensure_ascii=False) + "\n")
                written += 1
                added += 1

            log(f"[{i}/{len(group_lesson_ids)}] groupLessonId={glid}: +{added} rows")
            time.sleep(cfg.sleep)

    log(f"Generated {OBJECTIVES_JSONL_FILE}: {written} rows (errors: {errors})")
    return written


def post_change_multi_status(
    session: requests.Session,
    cfg: ClientConfig,
    group_id: int,
    student_id: int,
    levels_payload: List[Dict[str, Any]],
    dry_run: bool,
    log,
) -> Tuple[bool, int, Optional[Dict[str, Any]], Optional[str]]:
    url = CHANGE_MULTI_STATUS_URL_TEMPLATE.format(groupId=group_id, studentId=student_id)
    headers = {**default_headers_json(), "Content-Type": "application/json"}

    body = {
        "groupId": int(group_id),
        "levels": levels_payload,
        "studentId": int(student_id),
    }

    if dry_run:
        log(f"[DRY change-multi-status] url={url} levels={len(levels_payload)}")
        return True, 0, {"dry_run": True, "url": url, "levels_count": len(levels_payload)}, None

    try:
        r = session.post(url, headers=headers, cookies=cfg.cookies, json=body, timeout=cfg.timeout)
    except requests.RequestException as e:
        return False, 0, None, str(e)

    try:
        resp_json = r.json()
    except ValueError:
        resp_json = None

    ok = (200 <= r.status_code < 300)
    body_preview = None if resp_json is not None else r.text[:300]
    return ok, r.status_code, resp_json, body_preview


def step4_post_teacher_comments(cfg: ClientConfig, status_value: str, dry_run: bool, limit: int, log) -> Tuple[int, int, int]:
    if not os.path.exists(OBJECTIVES_JSONL_FILE):
        raise RuntimeError(f"Missing {OBJECTIVES_JSONL_FILE}. Run STEP 3 first.")

    session = make_session()
    headers = {**default_headers_json(), "Content-Type": "application/json"}

    processed = 0
    sent = 0
    errors = 0

    pending_levels: Dict[Tuple[int, int], Dict[int, Dict[str, Any]]] = defaultdict(dict)

    log(f"STEP 4: POST teacherComment/step | status={status_value!r} | dry_run={dry_run} | limit={limit or 'no limit'}")

    with open(OBJECTIVES_JSONL_FILE, "r", encoding="utf-8") as fin, open(POST_RESULTS_JSONL_FILE, "w", encoding="utf-8") as fres:
        for line in fin:
            line = line.strip()
            if not line:
                continue

            row = json.loads(line)
            hint_id = row.get("hint_id")
            level_id = row.get("level")
            student_id = row.get("student")

            if hint_id is None or level_id is None or student_id is None:
                continue

            payload = {
                "hint_id": str(hint_id),
                "level": int(level_id),
                "status": status_value,
                "student": int(student_id),
            }

            processed += 1
            if limit and processed > limit:
                break

            if dry_run:
                fres.write(json.dumps({"type": "teacherComment", "ok": True, "dry_run": True, "payload": payload}, ensure_ascii=False) + "\n")
                log(f"[DRY teacherComment] {payload}")
                time.sleep(cfg.sleep)

                group_id = row.get("groupId")
                lesson_id = row.get("lessonId")
                task_id = row.get("taskId")
                position = row.get("position")

                if group_id and lesson_id and task_id and position:
                    key = (int(group_id), int(student_id))
                    lvl_key = int(level_id)
                    pending_levels[key][lvl_key] = {
                        "attempts": 0,
                        "isPassed": True,
                        "lessonId": int(lesson_id),
                        "position": int(position),
                        "status": "complete",
                        "taskId": int(task_id),
                        "studentId": int(student_id),
                        "points": 0,
                    }
                else:
                    log(f"[WARN] Missing meta for change-multi-status (dry). student={student_id} levelId={level_id}")
                continue

            try:
                r = session.post(POST_URL, headers=headers, cookies=cfg.cookies, json=payload, timeout=cfg.timeout)
            except requests.RequestException as e:
                errors += 1
                fres.write(json.dumps({"type": "teacherComment", "ok": False, "error": str(e), "payload": payload}, ensure_ascii=False) + "\n")
                log(f"[ERR teacherComment] {payload} -> {e}")
                time.sleep(cfg.sleep)
                continue

            if r.status_code in (401, 403):
                fres.write(json.dumps({"type": "teacherComment", "ok": False, "http": r.status_code, "payload": payload, "body_preview": r.text[:300]}, ensure_ascii=False) + "\n")
                raise RuntimeError(f"Auth error on POST (HTTP {r.status_code}).")

            ok = (200 <= r.status_code < 300)
            if ok:
                sent += 1
            else:
                errors += 1

            try:
                resp_json = r.json()
            except ValueError:
                resp_json = None

            fres.write(json.dumps({
                "type": "teacherComment",
                "ok": ok,
                "http": r.status_code,
                "payload": payload,
                "response_json": resp_json,
                "body_preview": None if resp_json is not None else r.text[:300],
            }, ensure_ascii=False) + "\n")

            log(f"[teacherComment {r.status_code}] {payload}")

            if ok:
                group_id = row.get("groupId")
                lesson_id = row.get("lessonId")
                task_id = row.get("taskId")
                position = row.get("position")

                if group_id and lesson_id and task_id and position:
                    key = (int(group_id), int(student_id))
                    lvl_key = int(level_id)
                    pending_levels[key][lvl_key] = {
                        "attempts": 0,
                        "isPassed": True,
                        "lessonId": int(lesson_id),
                        "position": int(position),
                        "status": "complete",
                        "taskId": int(task_id),
                        "studentId": int(student_id),
                        "points": 0,
                    }
                else:
                    log(f"[WARN] Missing groupId/lessonId/taskId/position for change-multi-status. student={student_id} levelId={level_id}")

            time.sleep(cfg.sleep)

        log(f"STEP 4b: change-multi-status for {len(pending_levels)} (groupId,studentId) pairs")

        cms_ok = 0
        cms_err = 0

        for (group_id, student_id), levels_map in pending_levels.items():
            levels_payload = list(levels_map.values())

            ok2, http2, resp_json2, preview2 = post_change_multi_status(
                session=session,
                cfg=cfg,
                group_id=group_id,
                student_id=student_id,
                levels_payload=levels_payload,
                dry_run=dry_run,
                log=log,
            )

            if ok2:
                cms_ok += 1
                log(f"[change-multi-status OK] groupId={group_id} studentId={student_id} levels={len(levels_payload)}")
            else:
                cms_err += 1
                log(f"[change-multi-status ERR] groupId={group_id} studentId={student_id} http={http2} preview={preview2}")

            fres.write(json.dumps({
                "type": "change-multi-status",
                "ok": ok2,
                "http": http2,
                "groupId": group_id,
                "studentId": student_id,
                "levels_count": len(levels_payload),
                "response_json": resp_json2,
                "body_preview": preview2,
            }, ensure_ascii=False) + "\n")

            time.sleep(cfg.sleep)

        log(f"change-multi-status summary: ok={cms_ok}, err={cms_err}")

    log(f"POST results -> {POST_RESULTS_JSONL_FILE}")
    return processed, sent, errors


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Algoritmika Automation (Minimal GUI)")
        self.geometry("980x640")

        self._log_queue: "queue.Queue[str]" = queue.Queue()

        self.cookies_file_var = tk.StringVar(value="cookies.txt")
        self.groups_url_var = tk.StringVar(value=DEFAULT_GROUPS_URL)
        self.timeout_var = tk.StringVar(value="30")
        self.sleep_var = tk.StringVar(value="0.2")
        self.per_page_var = tk.StringVar(value="50")
        self.pages_per_group_var = tk.StringVar(value="1")

        self.post_status_var = tk.StringVar(value="success")
        self.post_dry_run_var = tk.BooleanVar(value=True)
        self.post_limit_var = tk.StringVar(value="0")

        self._build_ui()
        self.after(100, self._drain_log_queue)

    def _build_ui(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        top = ttk.LabelFrame(frm, text="Inputs")
        top.pack(fill=tk.X)

        row = 0
        ttk.Label(top, text="cookies.txt:").grid(row=row, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(top, textvariable=self.cookies_file_var, width=70).grid(row=row, column=1, sticky="we", padx=6, pady=6)
        ttk.Button(top, text="Browse...", command=self._browse_cookies).grid(row=row, column=2, padx=6, pady=6)
        row += 1

        ttk.Label(top, text="Groups URL:").grid(row=row, column=0, sticky="w", padx=6, pady=6)
        ttk.Entry(top, textvariable=self.groups_url_var, width=70).grid(row=row, column=1, sticky="we", padx=6, pady=6)
        ttk.Button(top, text="Use default", command=lambda: self.groups_url_var.set(DEFAULT_GROUPS_URL)).grid(row=row, column=2, padx=6, pady=6)
        row += 1

        settings = ttk.LabelFrame(frm, text="Settings")
        settings.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(settings, text="timeout(s)").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ttk.Entry(settings, textvariable=self.timeout_var, width=8).grid(row=0, column=1, padx=6, pady=6, sticky="w")

        ttk.Label(settings, text="sleep(s)").grid(row=0, column=2, padx=6, pady=6, sticky="w")
        ttk.Entry(settings, textvariable=self.sleep_var, width=8).grid(row=0, column=3, padx=6, pady=6, sticky="w")

        ttk.Label(settings, text="perPage").grid(row=0, column=4, padx=6, pady=6, sticky="w")
        ttk.Entry(settings, textvariable=self.per_page_var, width=8).grid(row=0, column=5, padx=6, pady=6, sticky="w")

        ttk.Label(settings, text="pages/group").grid(row=0, column=6, padx=6, pady=6, sticky="w")
        ttk.Entry(settings, textvariable=self.pages_per_group_var, width=8).grid(row=0, column=7, padx=6, pady=6, sticky="w")

        actions = ttk.LabelFrame(frm, text="Actions")
        actions.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(actions, text="Run ALL (1→2→3)", command=self._run_all).grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ttk.Button(actions, text="Step 1: groups -> groups_id.txt", command=self._run_step1).grid(row=0, column=1, padx=6, pady=6, sticky="w")
        ttk.Button(actions, text="Step 2: activity -> group_lesson_ids.txt", command=self._run_step2).grid(row=0, column=2, padx=6, pady=6, sticky="w")
        ttk.Button(actions, text="Step 3: lesson view -> objectives_to_review.jsonl", command=self._run_step3).grid(row=0, column=3, padx=6, pady=6, sticky="w")

        post = ttk.LabelFrame(frm, text="Optional Step 4: POST teacherComment/step (+ Step 4b change-multi-status)")
        post.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(post, text="status").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ttk.Entry(post, textvariable=self.post_status_var, width=10).grid(row=0, column=1, padx=6, pady=6, sticky="w")

        ttk.Checkbutton(post, text="Dry-run", variable=self.post_dry_run_var).grid(row=0, column=2, padx=6, pady=6, sticky="w")

        ttk.Label(post, text="limit (0=no limit)").grid(row=0, column=3, padx=6, pady=6, sticky="w")
        ttk.Entry(post, textvariable=self.post_limit_var, width=10).grid(row=0, column=4, padx=6, pady=6, sticky="w")

        ttk.Button(post, text="Run Step 4 POST", command=self._run_step4).grid(row=0, column=5, padx=6, pady=6, sticky="w")

        console_frame = ttk.LabelFrame(frm, text="Console")
        console_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.console = tk.Text(console_frame, height=18, wrap="word")
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(console_frame, command=self.console.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.console.configure(yscrollcommand=scrollbar.set)

        hint = ttk.Label(frm, text=f"Outputs: {GROUP_IDS_FILE}, {GROUP_LESSON_IDS_FILE}, {OBJECTIVES_JSONL_FILE}, {POST_RESULTS_JSONL_FILE}")
        hint.pack(anchor="w", pady=(8, 0))

    def _browse_cookies(self):
        path = filedialog.askopenfilename(title="Select cookies.txt", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.cookies_file_var.set(path)

    def _log(self, msg: str):
        self._log_queue.put(msg)

    def _drain_log_queue(self):
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.console.insert("end", msg + "\n")
                self.console.see("end")
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def _load_config(self) -> ClientConfig:
        cookies_path = self.cookies_file_var.get().strip()
        if not cookies_path or not os.path.exists(cookies_path):
            raise RuntimeError("cookies.txt not found. Choose correct file.")

        cookie_str = read_text_file(cookies_path)
        cookies = parse_cookie_header(cookie_str)
        if not cookies:
            raise RuntimeError("cookies.txt is empty or could not be parsed.")

        def parse_float(v: str, default: float) -> float:
            try:
                return float(v)
            except Exception:
                return default

        def parse_int(v: str, default: int) -> int:
            try:
                return int(v)
            except Exception:
                return default

        timeout = parse_float(self.timeout_var.get().strip(), 30.0)
        sleep_s = parse_float(self.sleep_var.get().strip(), 0.2)
        per_page = parse_int(self.per_page_var.get().strip(), 50)
        pages_per_group = parse_int(self.pages_per_group_var.get().strip(), 1)

        return ClientConfig(
            cookies=cookies,
            timeout=timeout,
            sleep=sleep_s,
            per_page=per_page,
            pages_per_group=max(1, pages_per_group),
        )

    def _run_in_thread(self, fn, title: str):
        def runner():
            try:
                self._log(f"--- {title} ---")
                fn()
                self._log(f"--- DONE: {title} ---")
            except Exception as e:
                self._log(f"ERROR: {e}")
                messagebox.showerror("Error", str(e))
        threading.Thread(target=runner, daemon=True).start()

    def _run_step1(self):
        def job():
            cfg = self._load_config()
            groups_url = self.groups_url_var.get().strip()
            step1_fetch_groups(groups_url, cfg, self._log)
        self._run_in_thread(job, "STEP 1")

    def _run_step2(self):
        def job():
            cfg = self._load_config()
            group_ids = read_ids(GROUP_IDS_FILE)
            if not group_ids:
                raise RuntimeError(f"{GROUP_IDS_FILE} is missing/empty. Run Step 1 first.")
            step2_fetch_group_lesson_ids(group_ids, cfg, self._log)
        self._run_in_thread(job, "STEP 2")

    def _run_step3(self):
        def job():
            cfg = self._load_config()
            group_lesson_ids = read_ids(GROUP_LESSON_IDS_FILE)
            if not group_lesson_ids:
                raise RuntimeError(f"{GROUP_LESSON_IDS_FILE} is missing/empty. Run Step 2 first.")
            step3_fetch_objectives(group_lesson_ids, cfg, self._log)
        self._run_in_thread(job, "STEP 3")

    def _run_all(self):
        def job():
            cfg = self._load_config()
            groups_url = self.groups_url_var.get().strip()
            group_ids = step1_fetch_groups(groups_url, cfg, self._log)
            group_lesson_ids = step2_fetch_group_lesson_ids(group_ids, cfg, self._log)
            step3_fetch_objectives(group_lesson_ids, cfg, self._log)
        self._run_in_thread(job, "RUN ALL (1→2→3)")

    def _run_step4(self):
        def job():
            cfg = self._load_config()
            status_value = self.post_status_var.get().strip() or "success"
            dry_run = bool(self.post_dry_run_var.get())

            try:
                limit = int(self.post_limit_var.get().strip() or "0")
            except Exception:
                limit = 0

            processed, sent, errors = step4_post_teacher_comments(
                cfg=cfg,
                status_value=status_value,
                dry_run=dry_run,
                limit=limit,
                log=self._log,
            )
            self._log(f"POST summary: processed={processed}, sent={sent}, errors={errors}")

        if not self.post_dry_run_var.get():
            if not messagebox.askyesno("Confirm", "Dry-run is OFF. This will send POST requests. Continue?"):
                return

        self._run_in_thread(job, "STEP 4 POST (+4b)")


if __name__ == "__main__":
    app = App()
    app.mainloop()
