import os.path
from datetime import datetime, timedelta
from functools import cache, cached_property

import requests
from tornado.web import HTTPError

from ultimaker_api.ultimaker import PrintJobPauseSources, PrinterStatus, PrintJobState


@cache
def get_printer_classes_by_type():
    """
    Gets all subclasses of Printer in a dictionary with the key their TYPE.
    """
    subclasses = {}
    def recurse(clazz):
        for subclass in clazz.__subclasses__():
            if hasattr(subclass, 'TYPE'):
                subclasses[subclass.TYPE] = subclass
            recurse(subclass)
    recurse(Printer)
    return subclasses


class PrinterHandlerMixin:
    def get_printer(self, name):
        return get_printer(name, self.settings['config'])


def get_printer(name, config):
    # Get the configuration for the printer
    if name not in config: raise HTTPError(404)
    config = config[name]
    if 'type' not in config: raise HTTPError(404)

    # Get the class to use for the printer
    cls = get_printer_classes_by_type().get(config['type'], Printer)

    # Create the printer object
    return cls(config)


def file_mod_datetime(path):
    return datetime.utcfromtimestamp(os.path.getmtime(path))


class Printer:
    def __init__(self, config):
        self.config = config

    @property
    def name(self): return self.config.name

    @property
    def status(self):
        """
        Gets the status of the printer which is always one of:
         * `'ready'` - printer is ready to print, sometimes called idle
         * `'printing'` - printer is actively printing (or starting to print,
                          resuming, finishing, cancelling, ...)
         * `'paused'` - printer is paused or pausing
         * `'done'` - printer is finished printing and waiting to be cleared
         * `'error'` - printer is undergoing maintenance or has a problem which
                       must be addressed before printing
         * `'unknown'` - printer is offline or its status is otherwise
                         unobtainable
        A printer may not support all of these states and group some together,
        at a minimum it should support 'ready' and 'printing'.
        """
        return 'unknown'


    @property
    def supports_video(self):
        """
        If this is True, the printer supports the `video_url` and `video_type`
        properties. Default is True if the printer's config includes a 'video'
        key. `video_type` may be 'unknown'.
        """
        return 'video' in self.config

    @property
    def video_url(self): return self.config['video']

    @property
    def video_type(self): return self.config.get('video_type', 'unknown')

    @property
    def video_settings(self):
        """
        A list of settings for the video that can include any of:
            'flipH' 'flipV' 'rotate90' 'rotate180' 'rotate270'
        Along with 0 or 1 aspect ratio:
            '16:9' '4:3' '3:2' '1:1'
        By default this returns the video_settings config setting (defaulting
        to an empty list).
        """
        return self.config.get('video_settings', '').split()


    @property
    def supports_link(self):
        """
        If this is True, the printer supports the `link` property. Default is
        True if the printer's config includes a 'link' key.
        """
        return 'link' in self.config

    @property
    def link(self): return self.config['link']


    @property
    def supports_gcode(self):
        """
        If this is True, the printer supports the `gcode` property and
        `is_up_to_date()` method. Default is always False.
        """
        return False

    @property
    def gcode(self):
        """
        Gets the currently printing complete gcode for the printer. Can block.
        """
        raise NotImplementedError()

    def is_up_to_date(self, path):
        """
        Checks if the path is up-to-date. A file is up-to-date if the path
        exists and has a timestamp after the printer start most recent print
        job.
        """
        return os.path.isfile(path) and (
                file_mod_datetime(path) > self.job_started)

    @property
    def supports_job(self):
        """
        If this is True, the printer supports the `job_*` properties. Default
        is always False.
        """
        return False

    @property
    def job_remaining_time(self):
        """
        Returns the number of seconds remaining (as a float, int, or timedelta)
        for the current print job.
        """
        raise NotImplementedError()

    @property
    def job_started(self):
        """
        Returns the time as a datetime when the current print job started.
        """
        raise NotImplementedError()


class Ultimaker(Printer):
    TYPE = 'ultimaker'

    def __init__(self, config):
        from ultimaker_api.ultimaker import Ultimaker
        if 'hostname' not in config: raise HTTPError(500)
        super().__init__(config)
        self.hostname = config['hostname']
        self.ultimaker = Ultimaker(self.hostname)

    @property
    def status(self):
        status = self.__status
        if status == PrinterStatus.PRINTING:
            status = self.__job["state"]
            if status in (PrintJobState.PRINTING, PrintJobState.RESUMING,
                          PrintJobState.PRE_PRINT, PrintJobState.POST_PRINT):
                return 'printing'
            elif status in (PrintJobState.PAUSED, PrintJobState.PAUSING):
                return 'paused'
            elif status in (PrintJobState.NO_JOB, PrintJobState.WAIT_CLEANUP,
                            PrintJobState.WAIT_USER_ACTION):
                return 'done'
            else:
                return 'unknown'
        elif status in (PrinterStatus.ERROR, PrinterStatus.MAINTENANCE,
                        PrinterStatus.BOOTING):
            return 'error'
        elif status == PrinterStatus.IDLE:
            return 'ready'
        else:
            return 'unknown'

    @property
    def supports_video(self): return True

    @property
    def video_url(self):
        return self.config.get('video',
                               f"http://{self.hostname}:8080/?action=stream")

    @property
    def video_type(self): return self.config.get('video_type', "MJPEG")

    @property
    def supports_link(self): return True

    @property
    def link(self):
        return self.config.get('link', f"http://{self.hostname}/print_jobs")

    @property
    def supports_gcode(self): return True

    @property
    def gcode(self): return self.ultimaker.print_job.gcode

    def is_up_to_date(self, path):
        return super().is_up_to_date(path) or self.__job['reprint_original_uuid']

    @property
    def supports_job(self): return True

    @property
    def job_remaining_time(self):
        return self.__job["time_total"] - self.__job["time_elapsed"]

    @property
    def job_started(self): return self.__job["datetime_started"]

    @cached_property
    def __status(self): return self.ultimaker.printer.status

    @cached_property
    def __job(self):
        """
        Obtains either the current job or the most recent historical job.

        Both types of jobs (current and historical) have:
          name, source, uuid, reprint_original_uuid, result
          datetime_started, datetime_finished, datetime_cleaned, time_elapsed, time_total
        A current job also has:
          progress     (filled in with 1.0 on a historical job)
          pause_source (filled in with PrintJobPauseSources.UNKNOWN on a historical job)
          state        (filled in with PrintJobState.NO_JOB on a historical job)
          source_user, source_application  (not filled in for historical jobs)
          (can also manually access gcode and container which are not available for historical jobs)
        A historical job also has:
          time_estimated  (not filled in for current jobs)
        """
        try:
            return self.ultimaker.print_job.dict
        except (KeyError, ValueError):  # when there is no current job return most recent job
            job = self.ultimaker.history.print_jobs[0].dict
            job['progress'] = 1.0
            job['pause_source'] = PrintJobPauseSources.UNKNOWN
            job['state'] = PrintJobState.NO_JOB
            return job


class Octopi(Printer):
    TYPE = 'octopi'

    def __init__(self, config):
        if 'hostname' not in config or 'apikey' not in config:
            raise HTTPError(500)
        super().__init__(config)
        self.hostname = config['hostname']
        self.apikey = config['apikey']

    @property
    def status(self):
        try:
            status = self.__status
        except (KeyError, ValueError):
            return 'unknown'  # unable to connect to printer
        if status.get("paused") or status.get("pausing"):
            return 'paused'
        elif status.get("printing") or status.get("resuming") or \
                status.get("finishing") or status.get("cancelling"):
            return 'printing'
        elif status.get("closedOrError") or status.get("error"):
            return 'error'
        elif status.get("operational") or status.get("ready"):
            return 'ready'
        else:
            return 'unknown'

    @property
    def supports_video(self):
        if 'video' in self.config: return True
        settings = self.__settings
        return "webcam" in settings and settings["webcam"]["webcamEnabled"] and settings["webcam"].get("streamUrl")

    @property
    def video_url(self):
        if 'video' in self.config: return self.config['video']
        return self.__settings["webcam"]["streamUrl"]

    @property
    def video_type(self):
        return self.config.get('video_type', 'MJPEG')  # defaults to MJPEG, but could be HLS...

    @property
    def video_settings(self):
        if 'video_settings' in self.config: return self.config['video_settings'].split()
        webcam_settings = self.__settings["webcam"]
        settings = [setting
                    for setting in ('flipH', 'flipV', 'rotate90', 'rotate180', 'rotate270')
                    if webcam_settings.get(setting, False)]
        if 'streamRatio' in webcam_settings: settings.append(webcam_settings['streamRatio'])
        return settings

    @property
    def supports_link(self): return True

    @property
    def link(self): return self.config.get('link', f"http://{self.hostname}/")

    @property
    def supports_gcode(self):
        job_file = self.__job_file
        return job_file is not None and "download" in job_file["refs"]

    @property
    def gcode(self):
        job_file = self.__job_file
        if job_file is None or "download" not in job_file["refs"]: return None
        return self.fetch(job_file["refs"]["download"], json=False).text

    @property
    def supports_job(self): return self.__job is not None

    @property
    def job_remaining_time(self):
        job = self.__job
        return 0 if job is None else job["progress"]["printTimeLeft"]

    @property
    def job_started(self):
        job = self.__job
        if job is None: return datetime.now()
        return datetime.now() - timedelta(seconds=job["progress"]["printTime"])

    @cached_property
    def __status(self):
        try:
            printer = self.get("printer")
        except ValueError as ex:
            if ex.args[0] == "Printer is not operational":
                return {"error":True}
            raise
        return printer["state"]["flags"]

    @cached_property
    def __settings(self): return self.get("settings")

    @cached_property
    def __job(self):
        if self.status in ("error", "unknown"): return None
        try: job = self.get("job")
        except ValueError: return None
        if job["state"] == "Operational":  # TODO
            file = self.__find_most_recent_file(self.__files["files"])
            if file is None: return None
            #job["job"]["lastPrintTime"] = ...
            job["job"]["file"]["name"] = file["name"]
            job["job"]["file"]["origin"] = file["origin"]
            job["job"]["file"]["path"] = file["path"]
            job["job"]["file"]["size"] = file.get("size", None)
            job["job"]["file"]["date"] = file.get("date", None)
            job["progress"]["completion"] = 1.0
            job["progress"]["filepos"] = file.get("size", None)
            job["progress"]["printTimeLeft"] = 0
            job["progress"]["printTimeLeftOrigin"] = 'linear'
            if "gcodeAnalysis" in file:
                est_print_time = file["gcodeAnalysis"]["estimatedPrintTime"]
                job["job"]["estimatedPrintTime"] = est_print_time
                job["progress"]["printTime"] = est_print_time
                job["job"]["filament"] = file["gcodeAnalysis"]["filament"]
            job["state"] == "Completed"
        return job

    def __find_most_recent_file(self, files):
        most_recent_file = None
        most_recent_date = 0
        for file in files:
            if file["type"] == "folder":
                file = self.__find_most_recent_file(file["children"])
            if file is None or file["type"] != "machinecode" or "prints" not in file:
                continue
            if most_recent_date < file["prints"]["last"]["date"]:
                most_recent_file = file
                most_recent_date = file["prints"]["last"]["date"]
        return most_recent_file

    @cached_property
    def __job_file(self):
        job = self.__job
        if job is None: return None
        file = job["job"]["file"]  # abridged information
        return self.get(f"files/{file['origin']}/{file['path']}") # full info

    @cached_property
    def __files(self): return self.get(f"files?recursive=true")

    def fetch(self, url, json=True):
        request = requests.get(
            url, headers={"X-Api-Key":self.apikey})
        if json:
            data = request.json()
            if "error" in data: raise ValueError(data["error"])
            return data
        return request

    def get(self, cmd):
        return self.fetch(f'http://{self.hostname}/api/{cmd}')
