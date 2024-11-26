#!/usr/bin/env python
# PYTHON_ARGCOMPLETE_OK
# coding: utf-8
# pylint: disable=E1101,R1732,C0301,C0302,W0603
"""
    run-para.py parallel ss commands
    Author: Franck Jouvanceau
"""
import os
import sys
import signal
import threading
import queue
import curses
from typing import Optional
from glob import glob
from re import sub, escape
from shlex import quote, split
from time import time, strftime, sleep
from datetime import timedelta, datetime
from subprocess import Popen, DEVNULL
from io import BufferedReader, TextIOWrapper
from argparse import ArgumentParser, Namespace, RawTextHelpFormatter
from dataclasses import dataclass
from copy import deepcopy
import argcomplete
from colorama import Fore, Style, init
from run_para.version import __version__
#__version__ = "0.1"

os.environ["TERM"] = "xterm-256color"

SYMBOL_END = os.environ.get("SSHP_SYM_BEG") or "\ue0b4"  # 
SYMBOL_BEGIN = os.environ.get("SSHP_SYM_END") or "\ue0b6"  # 
SYMBOL_PROG = os.environ.get("SSHP_SYM_PROG") or "\u25a0"  # ■
SYMBOL_RES = os.environ.get("SSHP_SYM_RES") or "\u25ba"  # b6 ▶
INTERRUPT = False
EXIT_CODE = 0

jobq = queue.Queue()
printq = queue.Queue()
pauseq = queue.Queue()


def shell_argcomplete(shell: str = "bash") -> None:
    """produce code to source in shell
    . <(run-para -C bash)
    run-para -C powershell | Out-String | Invoke-Expression
    """
    print(argcomplete.shell_integration.shellcode(["run-para"], shell=shell))
    sys.exit(0)


def log_choices(**kwargs) -> tuple:
    """argcomplete -L choices"""
    return (
        "*.status",
        "success.status",
        "failed.status",
        "killed.status",
        "timeout.status",
        "aborted.status",
        "*.out",
        "*.success",
        "*.failed",
        "params.list",
        "params_input.list",
        "run-para.log",
        "run-para.result",
        "run-para.command",
    )


def parse_args() -> Namespace:
    """argument parse"""
    if len(sys.argv) == 1:
        sys.argv.append("-h")
    parser = ArgumentParser(
        description=f"run-para v{__version__}", formatter_class=RawTextHelpFormatter
    )
    parser.add_argument("-V", "--version", action="store_true", help="run-para version")
    parser.add_argument(
        "-j", "--job", help="Job name added subdir to dirlog", default=""
    )
    parser.add_argument(
        "-d",
        "--dirlog",
        help="directory for ouput log files (default: ~/.run-para)",
        default=os.path.expanduser("~/.run-para"),
    )
    parser.add_argument(
        "-p", "--parallel", type=int, help="parallelism (default 4)", default=4
    )
    parser.add_argument("-t", "--timeout", type=int, help="timeout of each job")
    parser.add_argument(
        "-r", "--resolve", action="store_true", help="resolve fqdn in SSHP_DOMAINS"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose display (fqdn + line for last output)",
    )
    parser.add_argument(
        "-D",
        "--delay",
        type=float,
        default=0.3,
        help="initial delay in seconds between sh commands (default=0.3s)",
    )
    param_group = parser.add_mutually_exclusive_group()
    param_group.add_argument("-f", "--paramsfile", help="params list file")
    param_group.add_argument("-P", "--params", help="params list", nargs="+")
    param_group.add_argument(
        "-C",
        "--completion",
        choices=["bash", "zsh", "powershell"],
        help="autocompletion shell code to source",
    )
    param_group.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="list run-para results/log directories",
    )
    param_group.add_argument(
        "-L",
        "--logs",
        nargs="+",
        help="""get latest/current run-para run logs
-L[<runid>/]*.out          : all command outputs
-L[<runid>/]<param>.out    : command output for params
-L[<runid>/]*.<status>     : command output for params <status>
-L[<runid>/]*.status       : paraam lists with status
-L[<runid>/]<status>.status: <status> param list
-L[<runid>/]params.list     : list of params
default <runid> is latest run-para run (use -j <job> -d <dir> to access logs if used for run)
<status>: [success,failed,timeout,killed,aborted]
""",
    ).completer = log_choices  # type: ignore

    parser.add_argument("-s", "--script", help="script to execute")
    parser.add_argument("-a", "--args", nargs="+", help="script arguments")

    parser.add_argument("command", nargs="*")
    argcomplete.autocomplete(parser)
    return parser.parse_args()


def sigint_handler(*args) -> None:
    """exit all threads if signal"""
    global INTERRUPT
    INTERRUPT = True


def hometilde(directory: str) -> str:
    """substitute home to tilde in dir"""
    home = os.path.expanduser("~/")
    return sub(rf"^{escape(home)}", "~/", directory)


def addstr(stdscr: Optional["curses._CursesWindow"], *args, **kwargs) -> None:
    """curses addstr w/o exception"""
    if stdscr:
        try:
            stdscr.addstr(*args, **kwargs)
        except (curses.error, ValueError):
            pass


def addstrc(stdscr: Optional["curses._CursesWindow"], *args, **kwargs) -> None:
    """curses addstr and clear eol"""
    if stdscr:
        addstr(stdscr, *args, **kwargs)
        stdscr.clrtoeol()


def tdelta(*args, **kwargs) -> str:
    """timedelta without microseconds"""
    return str(timedelta(*args, **kwargs)).split(".", maxsplit=1)[0]


def print_tee(
    *args, file: Optional[TextIOWrapper] = None, color: str = "", **kwargs
) -> None:
    """print stdout + file"""
    print(" ".join([color] + list(args)), file=sys.stderr, **kwargs)
    if file:
        print(*args, file=file, **kwargs)


def decode_line(line: bytes) -> str:
    """try decode line exception on binary"""
    try:
        return line.decode()
    except UnicodeDecodeError:
        return ""


def last_line(fd: BufferedReader, maxline: int = 1000) -> str:
    """last non empty line of file"""
    line = "\n"
    fd.seek(0, os.SEEK_END)
    size = 0
    while line in ["\n", "\r"] and size < maxline:
        try:  # catch if file empty / only empty lines
            while fd.read(1) not in [b"\n", b"\r"]:
                fd.seek(-2, os.SEEK_CUR)
                size += 1
        except OSError:
            fd.seek(0)
            line = decode_line(fd.readline())
            break
        line = decode_line(fd.readline())
        fd.seek(-4, os.SEEK_CUR)
    return line.strip()



class Segment:
    """display of colored powerline style"""

    def __init__(
        self,
        stdscr: "curses._CursesWindow",
        nbsegments: int,
        bg: Optional[list] = None,
        fg: Optional[list] = None,
        style: Optional[list] = None,
        seg1: bool = True,
    ):
        """curses inits"""
        self.stdscr = stdscr
        self.segments = []
        self.nbsegments = nbsegments
        fg = fg or [curses.COLOR_WHITE] * nbsegments
        bg = bg or [
            curses.COLOR_BLUE,
            curses.COLOR_GREEN,
            curses.COLOR_RED,
            8,
            curses.COLOR_MAGENTA,
            curses.COLOR_CYAN,
            curses.COLOR_BLACK,
        ]
        bg[nbsegments] = curses.COLOR_BLACK
        self.st = style or ["NORMAL"] * nbsegments
        self.seg1 = seg1
        curses.init_pair(1, bg[0], curses.COLOR_BLACK)
        for i in range(0, nbsegments):
            curses.init_pair(i * 2 + 2, fg[i], bg[i])
            curses.init_pair(i * 2 + 3, bg[i], bg[i + 1])

    def set_segments(self, x: int, y: int, segments: list) -> None:
        """display powerline"""
        addstr(self.stdscr, y, x, SYMBOL_BEGIN, curses.color_pair(1))
        for i, segment in enumerate(segments):
            addstr(self.stdscr, f" {segment} ", curses.color_pair(i * 2 + 2))
            addstr(self.stdscr, SYMBOL_END, curses.color_pair(i * 2 + 3))
        self.stdscr.clrtoeol()


@dataclass
class JobStatus:
    """handle job statuses"""

    status: str = "IDLE"
    start: float = 0
    info: str = ""
    shortinfo: str = ""
    duration: float = 0
    pid: int = -1
    exit: Optional[int] = None
    logfile: str = ""
    log: str = ""
    thread_id: int = -1
    fdlog: Optional[BufferedReader] = None


class JobStatusLog:
    """manage log *.status files/count statuses"""

    @dataclass
    class LogStatus:
        """fd log/count status"""

        fd: Optional[TextIOWrapper] = None
        nb: int = 0

    def __init__(self, dirlog: str):
        """open log files for each status"""
        statuses = ["SUCCESS", "FAILED", "TIMEOUT", "KILLED", "ABORTED"]
        self.lstatus = {}
        for status in statuses:
            self.lstatus[status] = self.LogStatus(fd=self.open(dirlog, status))

    def open(self, dirlog: str, status: str) -> TextIOWrapper:
        """open log file for status"""
        return open(f"{dirlog}/{status.lower()}.status", "w", encoding="UTF-8")

    def addlog(self, info: str, status: str) -> None:
        """add info in status log"""
        if status in self.lstatus:
            self.lstatus[status].nb += 1
            print(info, file=self.lstatus[status].fd)

    def result(self) -> str:
        """print counts of statuses"""
        return " - ".join([f"{s.lower()}: {v.nb}" for s, v in self.lstatus.items()])

    def __del__(self):
        for s in self.lstatus.values():
            s.fd.close()


class JobPrint(threading.Thread):
    """
    Thread to display jobs statuses of JobRun threads
    """

    status_color = {
        "RUNNING": 100,
        "SUCCESS": 102,
        "FAILED": 104,
        "ABORTED": 104,
        "KILLED": 104,
        "TIMEOUT": 104,
        "IDLE": 106,
    }

    COLOR_GAUGE = 108
    COLOR_HOST = 110

    def __init__(
        self,
        command: list,
        nbthreads: int,
        nbjobs: int,
        dirlog: str,
        timeout: float = 0,
        verbose: bool = False,
        maxinfolen: int = 15,
    ):
        """init properties / thread"""
        super().__init__()
        self.th_status = [JobStatus() for i in range(nbthreads)]
        self.command = " ".join(command)
        self.cmd = self.command.replace("\n", "\\n")
        self.job_status = []
        self.nbthreads = nbthreads
        self.nbfailed = 0
        self.nbjobs = nbjobs
        self.dirlog = dirlog
        self.aborted = []
        self.startsec = time()
        self.stdscr: Optional[curses._CursesWindow] = None
        self.paused = False
        self.timeout = timeout
        self.verbose = verbose
        self.maxinfolen = maxinfolen
        self.killedpid = {}
        self.pdirlog = hometilde(dirlog)
        self.jobstatuslog = JobStatusLog(dirlog)
        if sys.stdout.isatty():
            self.init_curses()
        super().__init__()

    def __del__(self) -> None:
        self.print_summary()

    def init_curses(self) -> None:
        """curses window init"""
        self.stdscr = curses.initscr()
        curses.raw()
        # self.stdscr.scrollok(True)
        curses.noecho()
        curses.curs_set(0)
        curses.start_color()
        self.segment = Segment(self.stdscr, 5)
        curses.init_pair(
            self.status_color["RUNNING"], curses.COLOR_WHITE, curses.COLOR_BLUE
        )
        curses.init_pair(
            self.status_color["RUNNING"] + 1, curses.COLOR_BLUE, curses.COLOR_BLACK
        )
        curses.init_pair(
            self.status_color["SUCCESS"], curses.COLOR_WHITE, curses.COLOR_GREEN
        )
        curses.init_pair(
            self.status_color["SUCCESS"] + 1, curses.COLOR_GREEN, curses.COLOR_BLACK
        )
        curses.init_pair(
            self.status_color["FAILED"], curses.COLOR_WHITE, curses.COLOR_RED
        )
        curses.init_pair(
            self.status_color["FAILED"] + 1, curses.COLOR_RED, curses.COLOR_BLACK
        )
        curses.init_pair(self.status_color["IDLE"], curses.COLOR_WHITE, 8)
        curses.init_pair(self.status_color["IDLE"] + 1, 8, curses.COLOR_BLACK)
        curses.init_pair(self.COLOR_GAUGE, 8, curses.COLOR_BLUE)
        curses.init_pair(self.COLOR_HOST, curses.COLOR_YELLOW, curses.COLOR_BLACK)

    def killall(self) -> None:
        """kill all running threads pid"""
        for status in self.th_status:
            if status.status == "RUNNING":
                self.kill(status.thread_id)

    def run(self) -> None:
        """get threads status change"""
        jobsdur = 0
        nbcmdjobs = 0
        while True:
            if INTERRUPT:
                self.abort_jobs()
            try:
                jstatus: Optional[JobStatus] = printq.get(timeout=0.1)
            except queue.Empty:
                jstatus = None
            th_id = None
            if jstatus:
                if not jstatus.fdlog:  # start RUNNING
                    jstatus.fdlog = open(jstatus.logfile, "rb")
                jstatus.log = last_line(jstatus.fdlog)
                if jstatus.exit is not None:  # FINISHED
                    jstatus.fdlog.close()
                    jstatus.fdlog = None
                    nbcmdjobs += 1
                    jobsdur += jstatus.duration
                    if jstatus.status == "FAILED":
                        self.nbfailed += 1
                        if jstatus.pid in self.killedpid:
                            jstatus.status = self.killedpid[jstatus.pid]
                        if jstatus.exit == 255:
                            nbcmdjobs -= 1
                            jobsdur -= jstatus.duration
                        if INTERRUPT and jstatus.exit in [-2, 255, 4294967295]:
                            jstatus.status = "KILLED"
                            jstatus.exit = 256
                    self.jobstatuslog.addlog(jstatus.info, jstatus.status)
                    self.job_status.append(jstatus)
                self.th_status[jstatus.thread_id] = jstatus
                if not self.stdscr:
                    try:
                        print(
                            f"{strftime('%X')}: {jstatus.status} {len(self.job_status)}: {jstatus.info}"
                        )
                    except BrokenPipeError:
                        pass
            total_dur = tdelta(seconds=round(time() - self.startsec))
            if self.stdscr:
                self.display_curses(th_id, total_dur, jobsdur, nbcmdjobs)
            else:
                self.check_timeouts()
            if len(self.job_status) == self.nbjobs:
                break
        self.resume()
        global EXIT_CODE
        EXIT_CODE = 130 if INTERRUPT else (self.nbfailed > 0)
        if self.stdscr:
            addstrc(self.stdscr, curses.LINES - 1, 0, "All jobs finished")
            self.stdscr.refresh()
            self.stdscr.getch()
            curses.endwin()

    def check_timeout(self, th_id: int, duration: float) -> None:
        """kill cmd if duration exceeds timeout"""
        if not self.timeout:
            return
        if duration > self.timeout:
            self.kill(th_id, "TIMEOUT")

    def check_timeouts(self) -> None:
        """check threads timemout"""
        for i, jstatus in enumerate(self.th_status):
            if jstatus.status == "RUNNING":
                duration = time() - jstatus.start
                self.check_timeout(i, duration)

    def print_status(
        self, status: str, duration: float = 0, avgjobdur: float = 0
    ) -> None:
        """print thread status"""
        color = self.status_color[status]
        addstr(self.stdscr, SYMBOL_BEGIN, curses.color_pair(color + 1))
        if status == "RUNNING" and avgjobdur:
            pten = min(int(round(duration / avgjobdur * 10, 0)), 10)
            addstr(
                self.stdscr,
                SYMBOL_PROG * pten + " " * (10 - pten),
                curses.color_pair(self.COLOR_GAUGE),
            )  # ▶
        else:
            addstr(self.stdscr, f" {status:8} ", curses.color_pair(color))
        addstr(self.stdscr, SYMBOL_END, curses.color_pair(color + 1))
        addstr(self.stdscr, f" {tdelta(seconds=round(duration))}")

    def print_job(self, line_num: int, jstatus, duration: float, avgjobdur: float):
        """print info running on thread and last out line"""
        th_id = str(jstatus.thread_id).zfill(2)
        addstr(self.stdscr, line_num, 0, f" {th_id} ")
        self.print_status(jstatus.status, duration, avgjobdur)
        addstr(self.stdscr, f" {str(jstatus.pid):>7} ")
        if self.verbose:
            addstrc(self.stdscr, jstatus.info, curses.color_pair(self.COLOR_HOST))
            addstrc(self.stdscr, line_num + 1, 0, "     " + jstatus.log)
        else:
            addstr(
                self.stdscr,
                f"{jstatus.shortinfo:{self.maxinfolen}} {SYMBOL_RES} ",
                curses.color_pair(self.COLOR_HOST),
            )
            addstrc(self.stdscr, jstatus.log[: curses.COLS - self.stdscr.getyx()[1]])

    def display_curses(
        self, status_id: Optional[int], total_dur: str, jobsdur, nbcmdjobs
    ) -> None:
        """display threads statuses"""
        assert self.stdscr is not None
        nbend = len(self.job_status)
        last_start = 0
        avgjobdur = 0
        curses.update_lines_cols()
        self.get_key()
        if nbcmdjobs:
            avgjobdur = jobsdur / nbcmdjobs
        inter = self.verbose + 1
        line_num = 3
        nbrun = 0
        for jstatus in self.th_status:
            if jstatus.fdlog and jstatus.thread_id != status_id:
                jstatus.log = last_line(jstatus.fdlog)
            if jstatus.status == "RUNNING":
                duration = time() - jstatus.start
                self.check_timeout(jstatus.thread_id, duration)
                last_start = max(last_start, jstatus.start)
                nbrun += 1
                if curses.LINES > line_num + 1:
                    self.print_job(line_num, jstatus, duration, avgjobdur)
                    line_num += inter
            else:
                duration = jstatus.duration
        addstrc(self.stdscr, line_num, 0, "")
        if nbcmdjobs:
            last_dur = time() - last_start
            nbjobsq = max(min(self.nbthreads, nbrun), 1)
            estimated = tdelta(
                seconds=round(
                    max(avgjobdur * (self.nbjobs - nbend) / nbjobsq - last_dur, 0)
                )
            )
        else:
            estimated = ".:..:.."
        jobslabel = "paused" if self.paused else "pending"
        self.segment.set_segments(
            0,
            0,
            [
                f"running: {nbrun:>2} {jobslabel}: {self.nbjobs-nbend-nbrun}",
                f"done: {nbend}/{self.nbjobs}",
                f"failed: {self.nbfailed}",
                f"duration: {total_dur}",
                f"ETA: {estimated}",
            ],
        )
        addstr(self.stdscr, 1, 0, f" Dirlog: {self.pdirlog} Command: ")
        addstrc(self.stdscr, self.cmd[: curses.COLS - self.stdscr.getyx()[1]])
        addstrc(self.stdscr, 2, 0, "")
        self.print_finished(line_num + (nbrun > 0))
        if self.paused:
            addstrc(self.stdscr, curses.LINES - 1, 0, "[a]bort [k]ill [r]esume")
        else:
            addstrc(self.stdscr, curses.LINES - 1, 0, "[a]bort [k]ill [p]ause")
        self.stdscr.refresh()

    def get_key(self) -> None:
        """manage interactive actions"""
        global INTERRUPT
        assert self.stdscr is not None
        self.stdscr.nodelay(True)
        ch = self.stdscr.getch()
        self.stdscr.nodelay(False)
        # addstrc(self.stdscr, curses.LINES-1, 0, "===> "+str(ch))
        if ch == 97:  # a => abort (cancel)
            self.abort_jobs()
        if ch == 107:  # k kill
            self.curses_kill()
        if ch == 112:  # p pause
            self.pause()
        if ch == 114:  # r resume
            self.resume()
        if ch == 3:  # CTRL+c
            INTERRUPT = True
            self.abort_jobs()
            self.killall()

    def curses_kill(self) -> None:
        """interactive kill pid of thread"""
        curses.echo()
        assert self.stdscr is not None
        addstrc(self.stdscr, curses.LINES - 1, 0, "kill job in thread: ")
        try:
            th_id = int(self.stdscr.getstr())
        except ValueError:
            return
        finally:
            curses.noecho()
        self.kill(th_id)

    def kill(self, th_id, status="KILLED") -> None:
        """kill pid of thread id"""
        th_status = self.th_status[th_id]
        if th_status.pid > 0:
            try:
                os.kill(th_status.pid, signal.SIGINT)
                self.killedpid[th_status.pid] = status
            except ProcessLookupError:
                pass

    def pause(self) -> None:
        """pause JobRun threads"""
        if not self.paused:
            self.paused = True
            pauseq.put(True)

    def resume(self) -> None:
        """resume JobRun threads"""
        if self.paused:
            self.paused = False
            pauseq.get()
            pauseq.task_done()

    def print_finished(self, line_num: int) -> None:
        """display finished jobs"""
        assert self.stdscr is not None
        addstr(self.stdscr, curses.LINES - 1, 0, "")
        inter = self.verbose + 1
        for jstatus in self.job_status[::-1]:
            if curses.LINES < line_num + 2:
                break
            addstr(self.stdscr, line_num, 0, "")
            self.print_status(jstatus.status, jstatus.duration)
            addstr(self.stdscr, f" exit:{str(jstatus.exit):>3} ")
            if self.verbose:
                addstrc(self.stdscr, jstatus.info, curses.color_pair(self.COLOR_HOST))
                addstrc(self.stdscr, line_num + 1, 0, "     " + jstatus.log)
            else:
                addstr(
                    self.stdscr,
                    f"{jstatus.shortinfo:{self.maxinfolen}} {SYMBOL_RES} ",
                    curses.color_pair(self.COLOR_HOST),
                )
                addstrc(
                    self.stdscr, jstatus.log[: curses.COLS - self.stdscr.getyx()[1]]
                )
            line_num += inter
        self.stdscr.clrtobot()

    def abort_jobs(self) -> None:
        """aborts remaining jobs"""
        if not jobq.qsize():
            return
        while True:
            try:
                job = jobq.get(block=False)
                job.status.status = "ABORTED"
                job.status.exit = 256
                self.job_status.append(job.status)
                self.jobstatuslog.addlog(job.info, "ABORTED")
                jobq.task_done()
            except queue.Empty:
                break
            self.aborted.append(job.info)
        self.resume()

    def print_summary(self) -> None:
        """print/log summary of jobs"""
        end = strftime("%X")
        total_dur = tdelta(seconds=round(time() - self.startsec))
        global_log = open(f"{self.dirlog}/run-para.log", "w", encoding="UTF-8")
        print_tee("", file=global_log)
        nbrun = 0
        for jstatus in self.job_status:
            if jstatus.exit != 0:
                color = Style.BRIGHT + Fore.RED
            else:
                color = Style.BRIGHT + Fore.GREEN
            print_tee(f"{jstatus.status:8}:", color=color, file=global_log, end=" ")
            print_tee(jstatus.info, color=Fore.YELLOW, file=global_log, end=" ")
            if jstatus.status != "ABORTED":
                nbrun += 1
                print_tee(
                    f"exit: {jstatus.exit}",
                    f"dur: {tdelta(seconds=jstatus.duration)}",
                    f"{self.pdirlog}/{jstatus.info}.out",
                    file=global_log,
                )
            print_tee(" ", jstatus.log, file=global_log)
        print_tee("command:", self.command, file=global_log)
        print_tee("log directory:", self.pdirlog, file=global_log)
        start = datetime.fromtimestamp(self.startsec).strftime("%Y-%m-%d %H:%M:%S")
        print_tee(
            f"{nbrun}/{self.nbjobs} jobs run : begin: {start}",
            f"end: {end} dur: {total_dur}",
            file=global_log,
        )
        print_tee(self.jobstatuslog.result(), file=global_log)
        if self.nbfailed == 0:
            print_tee("All Jobs with exit code 0", file=global_log)
        else:
            print_tee(
                f"WARNING : {str(self.nbfailed)} Job(s) with exit code != 0",
                file=global_log,
                color=Style.BRIGHT + Fore.RED,
            )
        global_log.close()
        printfile(
            f"{self.dirlog}/run-para.result",
            f"begin: {start} end: {end} dur: {total_dur} runs: {nbrun}/{self.nbjobs} {self.jobstatuslog.result()}",
        )


class Job:
    """manage job execution"""

    def __init__(self, params: list, command: list):
        """job to run on info init"""
        self.params = params
        self.info = " ".join(params)
        self.status = JobStatus(info=self.info, shortinfo=self.info)
        self.jobcmd = []
        # map params to command to build the command to run
        for i,c in enumerate(command):
            self.jobcmd.append(c)
            if "@" in c:
                for j,p in enumerate(self.params):
                    v = f"@{j+1}"
                    if v in self.jobcmd[i]:
                        self.jobcmd[i] = self.jobcmd[i].replace(v, p)

    def exec(self, th_id: int, dirlog: str) -> None:
        """run command"""
        self.status.thread_id = th_id
        printfile(f"{dirlog}/{'_'.join(self.params)}.cmd", " ".join([quote(c) for c in self.jobcmd]))
        self.status.logfile = f"{dirlog}/{'_'.join(self.params)}.out"
        if dirlog:
            fdout = open(self.status.logfile, "w", encoding="UTF-8", buffering=1)
        else:
            fdout = sys.stdout
        self.status.start = time()
        pcmd = Popen(
            self.jobcmd,
            bufsize=0,
            encoding="UTF-8",
            stdout=fdout,
            stderr=fdout,
            stdin=DEVNULL,
            close_fds=True,
        )
        self.status.status = "RUNNING"
        self.status.pid = pcmd.pid
        printq.put(deepcopy(self.status))  # deepcopy to fix pb with object in queue
        pcmd.wait()
        fdout.close()
        self.status.exit = pcmd.returncode
        self.status.duration = time() - self.status.start
        self.status.status = "SUCCESS" if pcmd.returncode == 0 else "FAILED"
        printq.put(deepcopy(self.status))  # deepcopy to fix pb with object in queue
        with open(
            f"{dirlog}/{'_'.join(self.params)}.{self.status.status.lower()}", "w", encoding="UTF-8"
        ) as fstatus:
            print(
                "EXIT CODE:",
                self.status.exit,
                self.status.status,
                self.status.duration,
                file=fstatus,
            )


class JobRun(threading.Thread):
    """
    Threads launching jobs from rung in parallel
    """

    def __init__(self, thread_id: int, dirlog: str = ""):
        """constructor"""
        self.thread_id = thread_id
        self.dirlog = dirlog
        super().__init__()

    def run(self) -> None:
        """schedule Jobs / pause / resume"""
        while True:
            pauseq.join()
            if INTERRUPT:
                break
            try:
                job: Job = jobq.get(block=False)
            except queue.Empty:
                break
            job.exec(self.thread_id, self.dirlog)
            jobq.task_done()


def get_params(paramsfile: str, params: list) -> list:
    """returns infos list from args info or reading paramsfile"""
    if params:
        return [[p] for p in params]
    if not paramsfile:
        print("ERROR: run-para: No params definition", file=sys.stderr)
        sys.exit(1)
    if paramsfile == "-":
        return list(filter(len, [split(param) for param in sys.stdin.read().splitlines()]))
    try:
        with open(paramsfile, "r", encoding="UTF-8") as fparams:
            params = list(filter(len, [split(param) for param in fparams.read().splitlines()]))
    except OSError:
        print(f"ERROR: run-para: Cannot open {paramsfile}", file=sys.stderr)
        sys.exit(1)
    return params


def tstodatetime(ts) -> Optional[str]:
    """timestamp to datetime"""
    try:
        tsi = int(ts)
    except ValueError:
        return None
    return datetime.fromtimestamp(tsi).strftime("%Y-%m-%d %H:%M:%S")


def printfile(file: str, text: str) -> bool:
    """try print text to file"""
    try:
        with open(file, "w", encoding="UTF-8") as fd:
            print(text, file=fd)
    except OSError:
        return False
    return True


def readfile(file: str) -> Optional[str]:
    """try read from file"""
    try:
        with open(file, "r", encoding="UTF-8") as fd:
            text = fd.read()
    except OSError:
        return None
    return text.strip()


def log_results(dirlog: str, job: str) -> None:
    """print log results in dirlog/job"""
    if job:
        dirlog = f"{dirlog}/{job}"
    try:
        logdirs = os.listdir(dirlog)
    except OSError:
        print(f"no logs found in {dirlog}", file=sys.stderr)
        sys.exit(1)
    logdirs.sort()
    for logid in logdirs:
        result = readfile(f"{dirlog}/{logid}/run-para.result")
        command = readfile(f"{dirlog}/{logid}/run-para.command")
        if command:
            homelogid = f"{hometilde(dirlog)}/{logid:10}:"
            print(homelogid, result)
            print(len(homelogid) * " ", command)
    sys.exit(0)


def log_content(dirlog: str, wildcard: str) -> None:
    """print log file content in dirlog matching wildcard"""
    dirpattern = f"{dirlog}/{wildcard}"
    files = glob(dirpattern)
    files.sort()
    for logfile in files:
        if wildcard.split(".")[-1] in ["success", "failed"]:
            logfile = ".".join(logfile.split(".")[:-1]) + ".out"
        prefix = ""
        if len(files) > 1:
            prefix = logfile.split("/")[-1]
            if not prefix.startswith("run-para.") and not prefix.endswith("list."):
                prefix = prefix[:-4]
            prefix += ": "
        log = readfile(logfile)
        if log:
            log = log.splitlines()
            for line in log:
                print(prefix + line.rstrip())
            print()


def isdir(directory: str) -> bool:
    """test dir exits"""
    try:
        if os.path.isdir(directory):
            return True
    except OSError:
        return False
    return False


def get_latest_dir(dirlog: str) -> str:
    """retrieve last log dir"""
    try:
        dirs = glob(f"{dirlog}/[0-9]*")
    except OSError:
        print(f"Error: run-para: no log directory found in {dirlog}", file=sys.stderr)
        sys.exit(1)
    dirs.sort()
    for directory in dirs[::-1]:
        if isdir(directory):
            return directory
    print(f"no log directory found in {dirlog}")
    sys.exit(1)


def log_contents(wildcards: list, dirlog: str, job: str):
    """print logs content according to wildcards *.out *.success..."""
    if job:
        dirlog += f"/{job}"
    for wildcard in wildcards:
        if "/" in wildcard:
            logdir = dirlog + "/" + wildcard.split("/")[0]
            wildcard = wildcard.split("/")[1]
        else:
            logdir = get_latest_dir(dirlog)
        if not isdir(logdir):
            print(f"Notice: run-para: cannot access directory {logdir}")
            continue
        log_content(logdir, wildcard)
    sys.exit(0)


def make_latest(dirlog: str, dirlogtime: str) -> None:
    """make symlink to last log directory"""
    latest = f"{dirlog}/latest"
    try:
        if os.path.exists(latest):
            os.unlink(latest)
        os.symlink(dirlogtime, latest)
    except OSError:
        pass


def make_logdir(dirlog: str, job: str) -> str:
    """create log directory"""
    jobdirlog = dirlog
    if job:
        jobdirlog += f"/{job}"
    dirlogtime = jobdirlog + "/" + str(int(time()))
    try:
        if not os.path.isdir(dirlogtime):
            os.makedirs(dirlogtime)
    except OSError:
        print(f"Error: run-para: cannot create log directory: {dirlogtime}")
        sys.exit(1)
    make_latest(dirlog, dirlogtime)
    if job:
        make_latest(jobdirlog, dirlogtime)
    return dirlogtime


def main() -> None:
    """argument read / read params file / prepare commands / launch jobs"""
    init(autoreset=True)
    args = parse_args()
    if args.version:
        print(f"run-para: v{__version__}")
        sys.exit(0)
    if args.completion:
        shell_argcomplete(args.completion)
    if args.list:
        log_results(args.dirlog, args.job)
    if args.logs:
        log_contents(args.logs, args.dirlog, args.job)
    command = args.command
    if not args.command:
        print("Error: run-para: No command supplied", file=sys.stderr)
        sys.exit(1)
    if args.paramsfile:
        paramsfile = os.path.basename(args.paramsfile)
    else:
        paramsfile = "parameter"
    params = get_params(args.paramsfile, args.params)
    dirlog = make_logdir(args.dirlog, args.job)
    printfile(
        f"{dirlog}/run-para.command",
        f"Hostsfile: {paramsfile} Command: {' '.join(command)}",
    )
    printfile(f"{dirlog}/params.list", "\n".join([" ".join([quote(p) for p in par]) for par in params]))
    max_len = 0
    for param in params:
        max_len = max(max_len, len(" ".join(param)))

    for param in params:
        jobq.put(Job(params=param, command=args.command))
    parallel = min(len(params), args.parallel)
    signal.signal(signal.SIGINT, sigint_handler)
    try:
        signal.signal(signal.SIGPIPE, sigint_handler)
    except AttributeError:
        pass
    p = JobPrint(
        command, parallel, len(params), dirlog, args.timeout, args.verbose, max_len
    )
    p.start()
    jobruns = []
    for i in range(parallel):
        if jobq.qsize() == 0:
            break
        jobruns.append(JobRun(i, dirlog=dirlog))
        jobruns[i].start()
        sleep(args.delay)

    jobq.join()
    p.join()
    sys.exit(EXIT_CODE)


if __name__ == "__main__":
    main()
