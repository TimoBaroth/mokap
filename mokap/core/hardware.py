import time
import math
import numpy as np
import mokap.utils as utils
import os
from dotenv import load_dotenv
import pypylon.pylon as py
from typing import NoReturn, Union, List
from subprocess import  check_output
#import PySpin
#os.environ['SPINNAKER_GENTL64_CTI'] = '/Applications/Spinnaker/lib/spinnaker-gentl/Spinnaker_GenTL.cti'

import platform
import subprocess
import cv2

import warnings
from cryptography.utils import CryptographyDeprecationWarning
with warnings.catch_warnings():
    warnings.filterwarnings('ignore', category=CryptographyDeprecationWarning)
    import paramiko

import serial
import paho.mqtt.client as mqtt

##

def get_encoders(ffmpeg_path='ffmpeg', codec='hevc'):
    """
    Get available encoders for the given codec. This is tailored for h264 and hevc (h265)
    but this may work with others

    Parameters
    ----------
    codec: 'h265' or 'h264'

    Returns
    -------
    list of available encoders

    """
    if '265' in codec:
        codec = 'hevc'
    elif '264' in codec:
        codec = 'h264'

    r = check_output([ffmpeg_path, '-hide_banner', '-codecs'], stderr=False).decode('UTF-8').splitlines()
    codec_line = list(filter(lambda x: codec in x, r))[0]

    all_encoders = codec_line.split('encoders: ')[1].strip(')').split()

    encoders_names = []

    for encoder in all_encoders:
        r = check_output([ffmpeg_path, '-hide_banner', '-h', f'encoder={encoder}'], stderr=False).decode('UTF-8').splitlines()
        name_line = list(filter(lambda x: f'Encoder {encoder}' in x, r))[0]
        true_name = name_line.split(f'Encoder {encoder} [')[1][:-2]
        encoders_names.append(true_name)

    unique_encoders = {}
    for a, b in zip(all_encoders, encoders_names):
        if b not in unique_encoders:
            unique_encoders[b] = a
    return list(unique_encoders.values())


def setup_ulimit(wanted_value=8192, silent=True):
    """
        Sets up the maximum number of open file descriptors for nofile processes
        It is required to run multiple (i.e. more than 4) Basler cameras at a time
    """
    out = os.popen('ulimit')
    ret = out.read().strip('\n')

    if ret == 'unlimited':
        hard_limit = np.inf
    else:
        hard_limit = int(ret)

    out = os.popen('ulimit -n')
    current_limit = int(out.read().strip('\n'))

    if current_limit < wanted_value:
        if not silent:
            print(f'[WARN] Current file descriptors limit is too small (n={current_limit}), '
                  f'increasing it to {wanted_value} (max={hard_limit}).')
        os.popen(f'ulimit -n {wanted_value}')
    else:
        if not silent:
            print(f'[INFO] Current file descriptors limit seems fine (n={current_limit})')


def enable_usb(hub_number):
    """
        Uses uhubctl on Linux to enable the USB bus
    """
    if 'Linux' in platform.system():
        out = os.popen(f'uhubctl -l {hub_number} -a 1')
        ret = out.read()


def disable_usb(hub_number):
    """
        Uses uhubctl on Linux to disable the USB bus (effectively switches off the cameras connected to it
        so they don't overheat, without having to be physically unplugged)
    """
    if 'Linux' in platform.system():
        out = os.popen(f'uhubctl -l {hub_number} -a 0')
        ret = out.read()


def enumerate_basler_devices(virtual_cams=0) -> list[py.DeviceInfo]:

    instance = py.TlFactory.GetInstance()

    # List connected devices and get pointers to them
    dev_filter = py.DeviceInfo()

    dev_filter.SetDeviceClass("BaslerUsb")
    basler_devices = list(instance.EnumerateDevices([dev_filter, ]))

    if virtual_cams > 0:
        os.environ["PYLON_CAMEMU"] = f"{virtual_cams}"
        dev_filter.SetDeviceClass("BaslerCamEmu")
        basler_devices += list(instance.EnumerateDevices([dev_filter, ]))

    return basler_devices


def enumerate_flir_devices(virtual_cams=0):
    # TODO
    return []


def enumarate_webcam_devices():
    pltfm = platform.system().lower()

    if pltfm == "linux" or pltfm == "linux2":
        result = subprocess.run(["ls", "/dev/"],
                                stdout=subprocess.PIPE,
                                text=True)
        devices = [int(v.replace('video', '')) for v in result.stdout.split() if 'video' in v]
    elif pltfm == "darwin":
        # disgusting code block
        command = ['ffmpeg', '-f', 'avfoundation', '-list_devices', 'true', '-i', '""']
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        lines = result.stderr.splitlines()
        s = np.argwhere(['AVFoundation video devices:' in l for l in lines])[0][0]
        e = np.argwhere(['AVFoundation audio devices:' in l for l in lines])[0][0]
        devices = [int(l.split('[')[2][0]) for l in lines[s+1:e] if 'Capture screen' not in l]

    elif pltfm == "win32":
        result = subprocess.run(['pnputil', '/enum-devices', '/class', 'Camera', '/connected'],
                                stdout=subprocess.PIPE,
                                text=True)
        devices_ids = [v.replace('Instance ID:', '').strip() for v in result.stdout.splitlines() if 'Instance ID:' in v]
        devices = list(range(len(devices_ids)))
    else:
        raise OSError('Unsupported OS')

    working_ports = []

    prev_log_level = cv2.setLogLevel(0)

    for dev in devices:

        cap = cv2.VideoCapture(dev, 0, (cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY))

        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                working_ports.append(dev)
            cap.release()

    cv2.setLogLevel(prev_log_level)
    return working_ports


def ping(host: str) -> bool:

    if 'Windows' in platform.system():
        pop = subprocess.Popen(["ping", "-w", "1", "-n", "1", host], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        pop = subprocess.Popen(["ping", "-W", "1", "-c", "1", host], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pop.wait()
    if pop.returncode == 1:
        raise ConnectionError(f'{host} is unreachable :(')
    elif pop.returncode == 0:
        return True


##

class SSHTrigger:
    """
        Class to communicate with the hardware Trigger via SSH
        It uses the environment variables to load the host address and login info
    """

    def __init__(self, silent=False):

        self._connected = False
        self._silent = silent

        self.PWM_GPIO_PIN = 18  # Should be true for all Raspberry Pis

        load_dotenv()

        env_ip = os.getenv('TRIGGER_HOST')
        env_user = os.getenv('TRIGGER_USER')
        env_pass = os.getenv('TRIGGER_PASS')

        if None in (env_ip, env_user, env_pass):
            raise EnvironmentError(f'Missing {sum([v is None for v in (env_ip, env_user, env_pass)])} variables.')

        if ping(env_ip):

            # Open the connection to the Raspberry Pi
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.client.connect(env_ip, username=env_user, password=env_pass, look_for_keys=False)

            if self.client:
                self._connected = True
                if not self._silent:
                    print('[INFO] Trigger connected')
        else:
            print('[WARN] Trigger unreachable')

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self, frequency: float, highs_pct=50) -> NoReturn:
        """
            Starts the trigger loop on a Raspberry Pi
        """

        pct = int(np.floor(highs_pct * 1e4))
        frq = int(np.floor(frequency))

        if self.client is not None:
            self.client.exec_command(f'pigs hp {self.PWM_GPIO_PIN} {frq} {pct}')
            if not self._silent:
                print(f"[INFO] Trigger started at {frequency} Hz")

    def stop(self) -> NoReturn:
        if self.client:
            self.client.exec_command(f'pigs hp {self.PWM_GPIO_PIN} 0 0 && pigs w {self.PWM_GPIO_PIN} 0')
        time.sleep(0.1)
        if not self._silent:
            print(f"[INFO] Trigger stopped")

    def disconnect(self) -> NoReturn:
        if hasattr(self, 'client') and self.client:
            self.client.close()
            self.client = False

    def __del__(self):
        self.disconnect()



## 

class SerialTrigger:
    """
        Class to communicate with the hardware Trigger via Serial
        It uses the environment variables to load the COM port
    """

    def __init__(self, silent=False):

        self._connected = False
        self._silent = silent

        load_dotenv()

        env_com = os.getenv('TRIGGER_COMPORT')

        if env_com is None:
            raise EnvironmentError(f'Missing comport.')
        
        self.serialdevice = serial.Serial(port='COM4', baudrate=9600)
        if self.serialdevice:
            self._connected = True
            if not self._silent:
                print('[INFO] Trigger connected')
        else:
            print('[WARN] Trigger unreachable')

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self, frequency: float) -> NoReturn:
        """
            Starts the trigger on a MCU
        """
        frq = int(np.floor(frequency))

        if self.serialdevice  is not None:
            self.serialdevice.write((str(frq) + '\r\n').encode('utf-8'))
            if not self._silent:
                print(f"[INFO] Trigger started at {frq} Hz")

    def stop(self) -> NoReturn:
        if self.serialdevice:
            self.serialdevice.write((str(0) + '\r\n').encode('utf-8'))
        time.sleep(0.1)
        if not self._silent:
            print(f"[INFO] Trigger stopped")

    def disconnect(self) -> NoReturn:
        if hasattr(self, 'serialdevice') and self.serialdevice:
            self.serialdevice.close()
            self.serialdevice = False

    def __del__(self):
        self.disconnect()



##

class BaslerCamera:
    instancied_cams = []

    def __init__(self,
                 name='unnamed',
                 framerate=60,
                 exposure=5000,
                 triggered=True,
                 binning=1,
                 binning_mode='sum'):

        self._ptr = None
        self._dptr = None
        self._is_virtual = False

        self._serial = ''
        self._name = name

        self._width = 0
        self._height = 0
        self._probe_frame_shape = None  # (height, width)

        self._framerate = framerate
        self._exposure = exposure
        self._blacks = 0.0
        self._gain = 1.0
        self._gamma = 1.0
        self._triggered = triggered
        self._binning_value = binning
        self._binning_mode = binning_mode

        self._idx = -1

        self._connected = False
        self._is_grabbing = False

    def __repr__(self):
        if self._connected:
            v = 'Virtual ' if self._is_virtual else ''
            return f"Basler {v}Camera [S/N {self.serial}] (id={self._idx}, name={self._name})"
        else:
            return f"Basler Camera disconnected"

    def _set_roi(self) -> NoReturn:
        if not self._is_virtual:
            self._width = int(self.ptr.WidthMax.GetValue() - (16 // self._binning_value))
            self._height = int(self.ptr.HeightMax.GetValue() - (8 // self._binning_value))

            # Apply the dimensions to the ROI
            self.ptr.Width = self._width
            self.ptr.Height = self._height
            self.ptr.CenterX = True
            self.ptr.CenterY = True

        else:
            self._width = int(self._probe_frame_shape[1])
            self._height = int(self._probe_frame_shape[0])
            # self.ptr.Width = self._width
            # self.ptr.Height = self._height

    def connect(self, cam_ptr=None) -> NoReturn:

        available_idx = len(BaslerCamera.instancied_cams)

        if cam_ptr is None:
            real_cams, virtual_cams = enumerate_basler_devices()
            devices = real_cams + virtual_cams
            if available_idx <= len(devices):
                self._ptr = py.InstantCamera(py.TlFactory.GetInstance().CreateDevice(devices[available_idx]))
            else:
                raise RuntimeError("Not enough cameras detected!")
        else:
            self._ptr = cam_ptr

        self._dptr = self.ptr.DeviceInfo

        self.ptr.GrabCameraEvents = True
        self.ptr.Open()
        self._serial = self.dptr.GetSerialNumber()

        if '0815-0' in self.serial:
            self._is_virtual = True
            self._idx = max(available_idx, int(self.serial[-1]))
        else:
            self._is_virtual = False
            self._idx = available_idx

        if self._name in BaslerCamera.instancied_cams:
            self._name += f"_{self._idx}"

        self.ptr.UserSetSelector.SetValue("Default")
        self.ptr.UserSetLoad.Execute()
        self.ptr.AcquisitionMode.Value = 'Continuous'
        self.ptr.ExposureMode = 'Timed'

        self.ptr.DeviceLinkThroughputLimitMode.SetValue('On')
        # 342 Mbps is a bit less than the maximum, but things are more stable like this
        self.ptr.DeviceLinkThroughputLimit.SetValue(342000000)

        if self._probe_frame_shape is None:
            probe_frame = self.ptr.GrabOne(100)
            self._probe_frame_shape = probe_frame.GetArray().shape

        if not self._is_virtual:

            #self.ptr.ExposureTimeMode.SetValue("Standard") #throws "Node is not writable : AccessException error"
            self.ptr.ExposureAuto = 'Off'
            self.ptr.GainAuto = 'Off'
            self.ptr.TriggerDelay.Value = 0.0
            self.ptr.LineDebouncerTime.Value = 5.0
            self.ptr.MaxNumBuffer = 20

            self.ptr.TriggerSelector = "FrameStart"

            if self.triggered:
                self.ptr.LineSelector = "Line4"
                self.ptr.LineMode = "Input"
                self.ptr.TriggerMode = "On"
                self.ptr.TriggerSource = "Line4"
                self.ptr.TriggerActivation.Value = 'RisingEdge'
                self.ptr.AcquisitionFrameRateEnable.SetValue(False)
            else:
                self.ptr.TriggerMode = "Off"
                self.ptr.AcquisitionFrameRateEnable.SetValue(True)

        self._set_roi()
        
        

        self.binning = self._binning_value
        self.binning_mode = self._binning_mode

        self.framerate = self._framerate
        self.exposure = self._exposure
        self.blacks = self._blacks
        self.gain = self._gain
        self.gamma = self._gamma

        BaslerCamera.instancied_cams.append(self._name)
        self._connected = True

    def disconnect(self) -> NoReturn:
        if self._connected:
            if self._is_grabbing:
                self.stop_grabbing()

            self.ptr.Close()
            self._ptr = None
            self._dptr = None
            self._connected = False
            self._serial = ''
            self._name = 'unnamed'
            self._width = 0
            self._height = 0

        self._connected = False

    def set_userset(self, userset) -> NoReturn:
        if self._connected:
            self.ptr.UserSetSelector.SetValue(userset)
            self.ptr.UserSetLoad.Execute()
            

    def start_grabbing(self) -> NoReturn:
        if self._connected:
            if not self._is_grabbing:
                self.ptr.StartGrabbing()
                self._is_grabbing = True
        else:
            print(f"{self.name.title()} camera is not connected")

    def stop_grabbing(self) -> NoReturn:
        if self._connected:
            if self._is_grabbing:
                self.ptr.StopGrabbing()
                self._is_grabbing = False
        else:
            print(f"{self.name.title()} camera is not connected")
    @property
    def ptr(self) -> py.InstantCamera:
        return self._ptr

    @property
    def dptr(self) -> py.DeviceInfo:
        return self._dptr

    @property
    def idx(self) -> int:
        return self._idx

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, new_name: str) -> NoReturn:

        if new_name == self._name or self._name == f"{new_name}_{self._idx}":
            return
        if new_name not in BaslerCamera.instancied_cams and f"{new_name}_{self._idx}" not in BaslerCamera.instancied_cams:
            BaslerCamera.instancied_cams[BaslerCamera.instancied_cams.index(self._name)] = new_name
            self._name = new_name
        elif f"{self._name}_{self._idx}" not in BaslerCamera.instancied_cams:
            BaslerCamera.instancied_cams[BaslerCamera.instancied_cams.index(self._name)] = f"{self._name}_{self._idx}"
            self._name = f"{self._name}_{self._idx}"
        else:
            existing = BaslerCamera.instancied_cams.index(new_name)
            raise ValueError(f"A camera with the name {new_name} already exists: {existing}")    # TODO - handle this case nicely

    @staticmethod
    def pylon_exception_parser(exception) -> float:
        """
        Parses a Basler Pylon exception to get the adjusted camera parameter value

        Parameters
        ----------
        exception: Exception to parse

        Returns
        -------
        float
        The adjusted value
        """
        exception_message = exception.args[0]
        if 'must be smaller than or equal ' in exception_message:
            value = math.floor(100 * float(
                exception_message.split('must be smaller than or equal ')[1].split('. : OutOfRangeException')[
                    0])) / 100.0
        elif 'must be greater than or equal ' in exception_message:
            value = math.ceil(100 * float(
                exception_message.split('must be greater than or equal ')[1].split('. : OutOfRangeException')[
                    0])) / 100.0
        else:
            raise ValueError(f'[WARN] Unknown exception: {exception_message}')
        return value

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def triggered(self) -> bool:
        return self._triggered

    @property
    def exposure(self) -> int:
        return self._exposure

    @property
    def blacks(self) -> float:
        return self._blacks

    @property
    def gain(self) -> float:
        return self._gain

    @property
    def gamma(self) -> float:
        return self._gamma

    @property
    def binning(self) -> int:
        return self._binning_value

    @property
    def binning_mode(self) -> str:
        return self._binning_mode

    @binning.setter
    def binning(self, value: int):
        assert value in [1, 2, 3, 4]    # This should be all the possible values (for Basler cameras at least)
        # And keep a local value to avoid querying the camera every time we read it
        self._binning_value = value

        if self._connected:
            self.ptr.BinningVertical.SetValue(value)
            self.ptr.BinningHorizontal.SetValue(value)

        self._set_roi()

    @binning_mode.setter
    def binning_mode(self, value: str):
        if value.lower() in ['s', 'sum', 'add', 'addition', 'summation']:
            value = 'Sum'
        elif value.lower() in ['a', 'm', 'avg', 'average', 'mean']:
            value = 'Average'
        else:
            value = 'Sum'

        if self._connected:
            if not self._is_virtual:
                self.ptr.BinningVerticalMode.SetValue(value)
                self.ptr.BinningHorizontalMode.SetValue(value)

        # And keep a local value to avoid querying the camera every time we read it
        self._binning_mode = value

    @exposure.setter
    def exposure(self, value: float):
        if self._connected:
            try:
                if not self._is_virtual:
                    self.ptr.ExposureTime = value
                else:
                    self.ptr.ExposureTimeAbs = int(value)
                    self.ptr.ExposureTimeRaw = int(value)
            except py.OutOfRangeException as e:
                value = self.pylon_exception_parser(e)

                if not self._is_virtual:
                    self.ptr.ExposureTime = value
                else:
                    self.ptr.ExposureTimeAbs = int(value)
                    self.ptr.ExposureTimeRaw = int(value)

        # And keep a local value to avoid querying the camera every time we read it
        self._exposure = value

    @blacks.setter
    def blacks(self, value: float):
        if self._connected:
            try:
                self.ptr.BlackLevel.SetValue(value)
            except py.OutOfRangeException as e:
                value = self.pylon_exception_parser(e)
                self.ptr.BlackLevel.SetValue(value)
        # And keep a local value to avoid querying the camera every time we read it
        self._blacks = value

    @gain.setter
    def gain(self, value: float):
        if self._connected:
            try:
                self.ptr.Gain.SetValue(value)
            except py.OutOfRangeException as e:
                value = self.pylon_exception_parser(e)
                self.ptr.Gain.SetValue(value)
        # And keep a local value to avoid querying the camera every time we read it
        self._gain = value

    @gamma.setter
    def gamma(self, value: float):
        if self._connected:
            try:
                self.ptr.Gamma.SetValue(value)
            except py.OutOfRangeException as e:
                value = self.pylon_exception_parser(e)
                self.ptr.Gamma.SetValue(value)

        # And keep a local value to avoid querying the camera every time we read it
        self._gamma = value

    @property
    def framerate(self) -> float:
        return self._framerate

    @property
    def max_framerate(self) -> float:
        if not self._is_virtual:
            prev_state = self.ptr.AcquisitionFrameRateEnable.Value
            self.ptr.AcquisitionFrameRateEnable = False
            resulting_framerate = self.ptr.ResultingFrameRate.Value
            self.ptr.AcquisitionFrameRateEnable = prev_state
        else:
            resulting_framerate = 100.0
        return resulting_framerate

    @framerate.setter
    def framerate(self, value: float):
        if self._connected:
            if self.triggered:
                self.ptr.AcquisitionFrameRateEnable.SetValue(False)
                self._framerate = round(value, 2)
            else:
                new_framerate = round(min(value, self.max_framerate), 2)

                if self._is_virtual:
                    self.ptr.AcquisitionFrameRateAbs.SetValue(new_framerate)
                else:
                    self.ptr.AcquisitionFrameRate.SetValue(new_framerate)

                self._framerate = new_framerate

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def shape(self) -> np.array:
        # (height, width)
        return self._probe_frame_shape

    @property
    def temperature(self) -> Union[float, None]:
        if not self._is_virtual:
            try:
                val = self.ptr.DeviceTemperature.Value
                if val in [0.0, 421.0]:
                    return None
                else:
                    return val
            except py.AccessException:
                return None
        else:
            return None

    @property
    def temperature_state(self) -> str:
        if not self._is_virtual:
            return self.ptr.TemperatureState.Value
        else:
            return 'Ok'


##

class MQTTLogger:
    
    def __init__(self):
        
        load_dotenv()
        self.mqtt_ip = os.getenv('MQTT_HOST')
        self.mqtt_port =int(os.getenv('MQTT_PORT'))

        
        #MQTT topics => should be integrated in env/config, for now just copied here
        TCS_topic = "MEWRP4/CartridePrintHead/SET_CartridgeTemperature"
        TCA_topic = "MEWRP4/CartridePrintHead/ACT_CartridgeTemperature"
        TRS_topic = "MEWRP4/CartridePrintHead/SET_RingTemperature"
        TRA_topic="MEWRP4/CartridePrintHead/ACT_RingTemperature"
        PS_topic="MEWRP4/CartridePrintHead/SET_Pressure"
        PA_topic="MEWRP4/CartridgePrintHead/ACT_Pressure"
        HVS_topic="MEWRP4/HighVoltage/SET_HVSupplyVoltage"
        HVA_topic="MEWRP4/HighVoltage/ACT_HVSupplyVoltage"
        S_topic = "CAXIS/speed_act"

        #values 
        self.values = {
            "TCS": 0,
            "TCA": 0,
            "TRS": 0,
            "TRA": 0,
            "PS": 0,
            "PA": 0,
            "HVS": 0,
            "HVA": 0,
            "S": 0
            }
  
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,protocol=mqtt.MQTTv5)
        self.client.connect(self.mqtt_ip, self.mqtt_port) 
        self.client.loop_start()
        self.client.subscribe([(TCS_topic,2),(TCA_topic,2),(TRS_topic,2),(TRA_topic,2),(PS_topic,2),(PA_topic,2),(HVS_topic,2),(HVA_topic,2),(S_topic,2)])
        self.client.message_callback_add(TCS_topic, self.TCS_callback) # add callback for specific topic
        self.client.message_callback_add(TCA_topic, self.TCA_callback) # add callback for specific topic
        self.client.message_callback_add(TRS_topic, self.TRS_callback) # add callback for specific topic
        self.client.message_callback_add(TRA_topic, self.TRA_callback) # add callback for specific topic
        self.client.message_callback_add(PS_topic, self.PS_callback) # add callback for specific topic
        self.client.message_callback_add(PA_topic, self.PA_callback) # add callback for specific topic
        self.client.message_callback_add(HVS_topic, self.HVS_callback) # add callback for specific topic
        self.client.message_callback_add(HVA_topic, self.HVA_callback) # add callback for specific topic
        self.client.message_callback_add(S_topic, self.S_callback) # add callback for specific topic

    #for now just copied all the recall functions => should be unified 
    def TCS_callback(self, client, userdata, message):
        payload = message.payload.decode("utf-8")
        try: 
            self.values["TCS"] = float(payload)
        except:
            print("T_cartridge_set aquisition failed, received:" + payload)

    def TCA_callback(self, client, userdata, message):
        payload = message.payload.decode("utf-8")
        payload = payload.split(",")
        try: 
            self.values["TCA"] = float(payload[1]) #value
        except:
            self.values["TCA"] = -1 #indcate error/invalid value
            print("T_cartridge_act aquisition failed, received:" + payload)

    def TRS_callback(self, client, userdata, message):
        payload = message.payload.decode("utf-8")
        try: 
            self.values["TRS"] = float(payload)
        except:
            print("T_ring_set aquisition failed, received:" + payload)

    def TRA_callback(self, client, userdata, message):
        payload = message.payload.decode("utf-8")
        payload = payload.split(",")
        try: 
            self.values["TRA"] = float(payload[1])
        except:
            self.values["TRA"] = -1 #indcate error/invalid value
            print("T_ring_act aquisition failed, received:" + payload)

    def PS_callback(self, client, userdata, message):
        payload = message.payload.decode("utf-8")
        try: 
            self.values["PS"] = float(payload)
        except:
            print("P_set aquisition failed, received:" + payload)


    def PA_callback(self, client, userdata, message):
        payload = message.payload.decode("utf-8")
        payload = payload.split(",")
        try: 
            self.values["PA"] = float(payload[1])
        except:
            self.values["PA"] = -1 #indcate error/invalid value
            print("P_act aquisition failed, received:" + payload)


    def HVS_callback(self, client, userdata, message):
        payload = message.payload.decode("utf-8")
        try: 
            self.values["HVS"] = float(payload)
        except:
            print("HV_set aquisition failed, received:" + payload)

    def HVA_callback(self, client, userdata, message):
        payload = message.payload.decode("utf-8")
        payload = payload.split(",")
        try: 
            self.values["HVA"] = float(payload[1])
        except:
            self.values["HVA"] = -1 #indcate error/invalid value
            print("HV_act aquisition failed, received:" + payload)


    def S_callback(self, client, userdata, message):
        payload = message.payload.decode("utf-8")
        #print(payload)
        #payload = payload.split(",")
        try: 
            self.values["S"] = float(payload)
        except:
            self.values["S"] = -1
            print("speed aquisition failed, received:" + payload)


##
