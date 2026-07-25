class GPhoto2Error(Exception):
    def __init__(self, code, string=None):
        self.code = code
        self.string = string or f"gphoto2 error {code}"
        super().__init__(f"[{code}] {self.string}")


# gphoto2-result.h / gphoto2-port-result.h
GP_OK = 0
GP_ERROR = -1
GP_ERROR_BAD_PARAMETERS = -2
GP_ERROR_NOT_SUPPORTED = -6
GP_ERROR_IO = -7
GP_ERROR_TIMEOUT = -10
GP_ERROR_IO_INIT = -31
GP_ERROR_IO_READ = -34
GP_ERROR_IO_WRITE = -35
GP_ERROR_IO_USB_FIND = -52
GP_ERROR_IO_USB_CLAIM = -53
GP_ERROR_MODEL_NOT_FOUND = -105

# gphoto2-widget.h
GP_WIDGET_WINDOW = 0
GP_WIDGET_SECTION = 1
GP_WIDGET_TEXT = 2
GP_WIDGET_RANGE = 3
GP_WIDGET_TOGGLE = 4
GP_WIDGET_RADIO = 5
GP_WIDGET_MENU = 6
GP_WIDGET_BUTTON = 7
GP_WIDGET_DATE = 8

# gphoto2-camera.h
GP_EVENT_UNKNOWN = 0
GP_EVENT_TIMEOUT = 1
GP_EVENT_FILE_ADDED = 2
GP_EVENT_FOLDER_ADDED = 3
GP_EVENT_CAPTURE_COMPLETE = 4

GP_CAPTURE_IMAGE = 0
GP_CAPTURE_MOVIE = 1
GP_CAPTURE_SOUND = 2

# gphoto2-file.h
GP_FILE_TYPE_PREVIEW = 0
GP_FILE_TYPE_NORMAL = 1
GP_FILE_TYPE_RAW = 2
GP_FILE_TYPE_AUDIO = 3
GP_FILE_TYPE_EXIF = 4
GP_FILE_TYPE_METADATA = 5


class CameraFilePath:
    def __init__(self, folder="/store_00010001/DCIM/100MSDCF", name="DSC00001.ARW"):
        self.folder = folder
        self.name = name

    def __repr__(self):
        return f"CameraFilePath({self.folder!r}, {self.name!r})"


class Camera:
    def __init__(self):
        # Always fails: a test that gets here has escaped its double and would otherwise reach real USB.
        raise GPhoto2Error(GP_ERROR_MODEL_NOT_FOUND, "Unknown model (fake gphoto2)")
