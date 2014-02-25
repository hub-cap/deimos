#!/usr/bin/env python
import argparse
import collections
import os
import logging
import random
import sys
import time

import google.protobuf as pb

os.environ["GLOG_minloglevel"] = "3"        # Set before mesos module is loaded
import mesos
import mesos_pb2


#################################### Schedulers implement the integration tests

class Scheduler(mesos.Scheduler):
    def __init__(self, trials=10):
        self.token    = "%08x" % random.getrandbits(32)
        self.trials   = trials
        self.tasks    = []
        self.statuses = {}
        self.log      = log.getChild("scheduler")
        self.loggers  = {}
    def __repr__(self):
        return "%s(%r)" % (self.__class__, self.__dict__)
    def registered(self, driver, framework_id, master):
        self.framework_id = framework_id
        self.log.info("Registered with ID:\n  %s" % framework_id.value)
    def statusUpdate(self, driver, update):
        task, code = update.task_id.value, update.state
        self.statuses[task] = code
        self.loggers[task].info(present_status(update))
    def all_tasks_done(self):
        agg = [_ for _ in self.statuses.values() if _ in Scheduler.terminal]
        return len(agg) >= self.trials
    def sum_up(self):
        sums = [ "%s=%d" % (k, v) for k, v in self.task_status_summary() ]
        log.info(" ".join(sums))
    def task_status_summary(self):
        counts = collections.defaultdict(int)
        for task, code in self.statuses.items():
            counts[code] += 1
        return [ (mesos_pb2.TaskState.Name(code), count)
                 for code, count in counts.items() ]
    def next_task_id(self):
        short_id = "%s-task%02d" % (self.token, len(self.tasks))
        long_id  = "medea-test." + short_id
        self.loggers[long_id] = log.getChild(short_id)
        return long_id
    terminal = set([ mesos_pb2.TASK_FINISHED,
                     mesos_pb2.TASK_FAILED,
                     mesos_pb2.TASK_KILLED,
                     mesos_pb2.TASK_LOST ])
    failed   = set([ mesos_pb2.TASK_FAILED,
                     mesos_pb2.TASK_KILLED,
                     mesos_pb2.TASK_LOST ])

class ExecutorScheduler(Scheduler):                # TODO: Make this class work
    def __init__(self, command, uris=[], container=None, trials=10):
        Scheduler.__init__(self, trials)
        self.command   = command
        self.uris      = uris
        self.container = container
        self.messages  = []
    def statusUpdate(self, driver, update):
        super(ExecutorScheduler, self).statusUpdate(driver, update)
        if update.state == mesos_pb2.TASK_RUNNING:
            pass                  # TODO: Send a message if we get TASK_RUNNING
        if self.all_tasks_done():
            self.sum_up()
            driver.stop()
    def frameworkMessage(self, driver, executor_id, slave_id, msg):
        self.messages += [msg]
        driver.killTask(update.task_id)
    def resourceOffers(self, driver, offers):
        for offer in offers:
            if len(self.tasks) >= self.trials: break
            tid  = self.next_task_id()
            sid  = offer.slave_id
            task = task_with_executor(tid, sid)
            self.tasks += [task]
            self.loggers[tid].info(present_task(task))
            driver.launchTasks(offer.id, [task])

class SleepScheduler(Scheduler):
    wiki = "https://en.wikipedia.org/wiki/Main_Page"
    def __init__(self, sleep=10, uris=[wiki], container=None, trials=5):
        Scheduler.__init__(self, trials)
        self.sleep     = sleep
        self.uris      = uris
        self.container = container
        self.done      = []
    def statusUpdate(self, driver, update):
        super(SleepScheduler, self).statusUpdate(driver, update)
        if self.all_tasks_done():
            self.sum_up()
            driver.stop()
    def resourceOffers(self, driver, offers):
        delay = int(float(self.sleep) / self.trials)
        for offer in offers:
            if len(self.tasks) >= self.trials: break
          # time.sleep(self.sleep + 0.5)
            time.sleep(delay)                    # Space out the requests a bit
            tid  = self.next_task_id()
            sid  = offer.slave_id
            cmd  = "date -u +%T ; sleep " + str(self.sleep) + " ; date -u +%T"
            task = task_with_command(tid, sid, cmd, self.uris, self.container)
            self.tasks += [task]
            self.loggers[tid].info(present_task(task))
            driver.launchTasks(offer.id, [task])

class PGScheduler(Scheduler):
    def __init__(self, container="docker:///zaiste/postgresql", trials=10):
        Scheduler.__init__(self, trials)
        self.container = container
    def statusUpdate(self, driver, update):
        super(PGScheduler, self).statusUpdate(driver, update)
        if update.state == mesos_pb2.TASK_RUNNING:
            time.sleep(2)
            driver.killTask(update.task_id)       # Shutdown Postgres container
        if self.all_tasks_done():
            self.sum_up()
            driver.stop()
    def resourceOffers(self, driver, offers):
        for offer in offers:
            if len(self.tasks) >= self.trials: break
            time.sleep(2)
            tid  = self.next_task_id()
            sid  = offer.slave_id
            task = task_with_daemon(tid, sid, self.container)
            self.tasks += [task]
            self.loggers[tid].info(present_task(task))
            driver.launchTasks(offer.id, [task])


################################################################ Task factories

def task_with_executor(tid, sid, *args):
    executor = mesos_pb2.ExecutorInfo()
    executor.executor_id.value = tid
    executor.name = tid
    executor.source = "medea-test"
    executor.command.MergeFrom(command(*args))
    task = task_base(tid, sid)
    task.executor.MergeFrom(executor)
    return task

def task_with_command(tid, sid, *args):
    task = task_base(tid, sid)
    task.command.MergeFrom(command(*args))
    return task

def task_with_daemon(tid, sid, image):
    task = task_base(tid, sid)
    task.command.MergeFrom(command(image=image))
    return task

def task_base(tid, sid, cpu=0.5, ram=256):
    task = mesos_pb2.TaskInfo()
    task.task_id.value = tid
    task.slave_id.value = sid.value
    task.name = tid
    cpus = task.resources.add()
    cpus.name = "cpus"
    cpus.type = mesos_pb2.Value.SCALAR
    cpus.scalar.value = cpu
    mem = task.resources.add()
    mem.name = "mem"
    mem.type = mesos_pb2.Value.SCALAR
    mem.scalar.value = ram
    return task

def command(shell="", uris=[], image=None):
    command = mesos_pb2.CommandInfo()
    command.value = shell
    for uri in uris:
        command.uris.add().value = uri
    if image:                      # Rely on the default image when none is set
        container = mesos_pb2.CommandInfo.ContainerInfo()
        container.image = image
        command.container.MergeFrom(container)
    return command

def present_task(task):
    if task.HasField("executor"):
        token, body = "executor", task.executor
    else:
        token, body = "command", task.command
    lines = pb.text_format.MessageToString(body).strip().split("\n")
    return "\n  %s {\n    %s\n  }" % (token, "\n    ".join(lines))

def present_status(update):
    info = mesos_pb2.TaskState.Name(update.state)
    if update.state in Scheduler.failed and update.HasField("message"):
        info += '\n  message: "%s"' % update.message
    return info


########################################################################## Main

def cli():
    schedulers = { "sleep" : SleepScheduler,
                   "pg"    : PGScheduler }
    p = argparse.ArgumentParser(prog="medea-test.py")
    p.add_argument("--master", default="localhost:5050",
                   help="Mesos master URL")
    p.add_argument("--test", choices=schedulers.keys(), default="sleep",
                   help="Test suite to use")
    p.add_argument("--test.container",
                   help="Image URL to use (for any test)")
    p.add_argument("--test.sleep", type=int,
                   help="Seconds to sleep (for sleep test)")
    p.add_argument("--test.trials", type=int,
                   help="Number of tasks to run (for any test)")
    p.add_argument("--test.command",
                   help="Command to use (for executor test)")
    p.add_argument("--test.uris", action="append",
                   help="Pass any number of times to add URIs (for any test)")
    parsed = p.parse_args()

    pairs = [ (k.split("test.")[1:], v) for k, v in vars(parsed).items() ]
    constructor_args = dict( (k[0], v) for k, v in pairs if len(k) == 1 and v )
    scheduler_class = schedulers[parsed.test]
    scheduler = scheduler_class(**constructor_args)
    args = ", ".join( "%s=%r" % (k, v) for k, v in constructor_args.items() )
    scheduler.log.info("Using %s(%s)" % (scheduler_class.__name__, args))

    framework = mesos_pb2.FrameworkInfo()
    framework.name = "medea-test"
    framework.user = ""
    driver = mesos.MesosSchedulerDriver(scheduler, framework, parsed.master)
    code = driver.run()
    log.info(mesos_pb2.Status.Name(code))
    driver.stop()
    ################  2 => driver problem  1 => tests failed  0 => tests passed
    if code != mesos_pb2.DRIVER_STOPPED:
        log.error("Driver died in an anomalous state")
        os._exit(2)
    if any(_ in Scheduler.failed for _ in scheduler.statuses.values()):
        log.error("Test run failed -- not all tasks made it")
        os._exit(1)
    os._exit(0)

logging.basicConfig(format="%(asctime)s.%(msecs)03d %(name)s %(message)s",
                    datefmt="%H:%M:%S", level=logging.DEBUG)
log = logging.getLogger("medea-test")

if __name__ == "__main__": cli()
