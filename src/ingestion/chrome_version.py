"""Select ChromeDriver for the installed browser, shared by both crawlers."""
import logging
import os

if __package__:
    from .ingestion_runtime import make_chrome as _make_chrome
    from .browser_health import configure_transport
else:
    from ingestion_runtime import make_chrome as _make_chrome
    from browser_health import configure_transport

LOG = logging.getLogger(__name__)


def installed_chrome_major(executable):
    """Read the installed binary version, not the newest downloadable driver."""
    if not executable:
        raise RuntimeError('Không tìm thấy Chrome. Cài Chrome hoặc chỉ rõ đường dẫn trình duyệt.')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class FixedVersion(ctypes.Structure):
            _fields_ = [('signature', wintypes.DWORD), ('structure_version', wintypes.DWORD),
                        ('file_version_ms', wintypes.DWORD), ('file_version_ls', wintypes.DWORD)]
        api = ctypes.windll.version
        size = api.GetFileVersionInfoSizeW(str(executable), None)
        if size:
            buffer = ctypes.create_string_buffer(size)
            if api.GetFileVersionInfoW(str(executable), 0, size, buffer):
                pointer = ctypes.c_void_p()
                length = wintypes.UINT()
                if api.VerQueryValueW(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)):
                    info = ctypes.cast(pointer, ctypes.POINTER(FixedVersion)).contents
                    if info.signature == 0xFEEF04BD:
                        return info.file_version_ms >> 16
    else:
        import re
        import subprocess
        result = subprocess.run([str(executable), '--version'], capture_output=True, text=True, timeout=10)
        match = re.search(r'\b(\d+)\.\d+\.\d+\.\d+', result.stdout)
        if match:
            return int(match.group(1))
    raise RuntimeError('Không đọc được phiên bản Chrome; truyền --chrome-major theo phiên bản đang cài.')



def make_chrome(source, *, options=None, chrome_major=None, **kwargs):
    import undetected_chromedriver as uc
    options = options or uc.ChromeOptions()
    # Both crawlers can run in parallel; keep the inactive window's renderer
    # and AJAX timers running while the other source is in the foreground.
    for flag in ('--disable-background-timer-throttling',
                 '--disable-renderer-backgrounding',
                 '--disable-backgrounding-occluded-windows'):
        if flag not in options.arguments:
            options.add_argument(flag)
    executable = options.binary_location or uc.find_chrome_executable()
    if executable:
        options.binary_location = executable
    if chrome_major is None:
        chrome_major = installed_chrome_major(executable)
        LOG.info('Dùng ChromeDriver cùng phiên bản Chrome đã cài: %s.', chrome_major)
    return configure_transport(_make_chrome(source, options=options, chrome_major=chrome_major, **kwargs))
