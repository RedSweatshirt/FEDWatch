import serial
import signal
import time
import datetime
import multiprocessing as mp
import pandas as pd
import os
import sys
import configparser
import yagmail
from yagmail.error import YagInvalidEmailAddress
import keyring
from smtplib import SMTPServerDisconnected, SMTPAuthenticationError
import tkinter as tk
import tkinter.filedialog
import requests
import warnings

# add simple formatting for the warnings
# this avoids the actual code being printed twice
def simple_format(message, category, filename, lineno, line=None):
    return f"{filename}:{lineno}: {category.__name__}: {message}\n"

warnings.formatwarning = simple_format

class Fedwatcher:
    # bitrate of serial from fed to pi
    ### do not set above 57600, will lose data ###
    baud = 57600

    # number of seconds you want each port to wait for a response
    timeout = 1

    # Port variables
    portpaths = tuple()
    ports = []

    # Process variables
    run_process = None
    ready = False
    running = False

    # Multiprocessing variables
    manager = None
    port_locks = None
    main_thread = False

    # Dataframe variables
    columns = ["Pi_Time", "MM:DD:YYYY hh:mm:ss", "Library_Version", "Session_type", "Device_Number", "Battery_Voltage", "Motor_Turns", "FR", "Event", "Active_Poke", "Left_Poke_Count", "Right_Poke_Count", "Pellet_Count", "Block_Pellet_Count", "Retrieval_Time", "InterPellet_Retrieval_Time", "Poke_Time"]
    data_queue = None
    df_dict = {}

    # Saving variables
    save_interval = 300 # seconds between df saves
    max_size = 100 # max entries before a df is saved and emptied
    last_save = None
    configpath = "../config.yaml"
    exp_dir = "Documents"
    today_dir = ''
    exp_name = "Fedwatcher"
    session_num = 0

    # Email variables
    email_enabled = False

    # Notification variables
    last_notif = None
    # TODO: give user control using GUI.py
    notif_interval = 6 # in hours

    def __init__(self, baud=57600, timeout=1, 
        portpaths = ("/dev/serial0", "/dev/ttyAMA1", "/dev/ttyAMA2", "/dev/ttyAMA3", "/dev/ttyAMA4"), 
        configpath = os.path.expanduser("~/FEDWatcher/fedwatcher/config.yaml"), 
        tg_enabled = False):
        """
        Constructor
        Creates a new Fedwatch object with baud, timeout, and portpaths
        Arguments:
            baud: bitrate of serial connection from FED3. Will have errors if above 57600. Must match FED3 baud
            timeout: number of seconds to wait upon a readline call before stopping
            portpaths: the path to each open serial port on the Raspberry Pi. Defaulted to opening UART2 through UART5 in order
            configpath: the path from user to the config.yaml file. For example, if it is at ~/FEDWatcher/fedwatcher/config.yaml, give FEDWatcher/fedwatcher/config.yaml
        """
        self.baud = baud
        self.timeout = timeout
        self.portpaths = portpaths
        self.tg_enabled = tg_enabled
        self.manager = mp.Manager()
        self.port_locks = self.manager.list()
        self.last_save = time.time()
        self.data_queue = mp.Queue()
        self.open_portpaths = []

        if configpath is not None:
            self.configpath = configpath
            self.check_config()

        # Makes it so that on receiving a terminate signal, saves all data
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

        # Mapping of port paths to their corresponding GPIO pins for RX (Receive)
        port_to_gpio = {
            "/dev/serial0": "GPIO 14 (TX) & GPIO 15 (RX)",  # These are default UART pins, might change based on configuration
            "/dev/ttyAMA1": "GPIO 14 (TX) & GPIO 15 (RX)",  # Check if this maps differently in your setup
            "/dev/ttyAMA2": "GPIO 0 (TX) & GPIO 1 (RX)",
            "/dev/ttyAMA3": "GPIO 4 (TX) & GPIO 5 (RX)",
            "/dev/ttyAMA4": "GPIO 8 (TX) & GPIO 9 (RX)"
        }
        print("Trying to connect to ports. FEDWatcher only listens to RX GPIO pins!")
        print(port_to_gpio)
        for portpath in self.portpaths:
            try:
                port = serial.Serial(
                    port = portpath,
                    baudrate = self.baud,
                    parity = serial.PARITY_NONE,
                    stopbits = serial.STOPBITS_ONE,
                    bytesize = serial.EIGHTBITS,
                    timeout = self.timeout,
                )
                if port.is_open:
                    gpio_info = port_to_gpio.get(portpath, "Unknown GPIO")
                    self.ports.append(port)
                    self.open_portpaths.append(portpath)
                    self.port_locks.append(False)
                    print(f"Connected to {portpath} using {gpio_info}")
                else:
                    print(f"[WARNING]: Failed to connect to {portpath}")
                    #raise IOError("Serial port at % not opening" % portpath)
            except Exception as e:
                print(f"Error opening {portpath}: {e}")
                if portpath == "/dev/ttyAMA1" and "/dev/serial0" in self.open_portpaths:
                    print("[INFO]: /dev/ttyAMA1 cannot be used if using /dev/serial0")

        if self.ports:
            self.ready = True
            self.ports = tuple(self.ports)
        else:
            raise RuntimeError("Not able to connect to any ports or no ports given. \
                Try going through setup process, make sure serial ports are enabled in boot/config.txt \
                or make sure that given serial port paths are correct.")

    def setupNewPorts(self, portpaths):
        """
        Used to change the active ports to the ones given in portpaths.
        Will close ports currently open
        Arguments:
            portpaths: new list of ports to use
        """

        if self.running:
            raise RuntimeError("Process is running, cannot call")

        if portpaths is not None:
            if not portpaths:
                raise RuntimeError("Given empty portpaths")
            self.close()
            self.ports = tuple()
            self.portpaths = portpaths

            for portpath in self.portpaths:
                port = serial.Serial(
                    port = portpath,
                    baudrate = self.baud,
                    parity = serial.PARITY_NONE,
                    stopbits = serial.STOPBITS_ONE,
                    bytesize = serial.EIGHTBITS,
                    timeout = self.timeout,
                )
                if not port.is_open:
                    raise IOError("Serial port at % not opening" % portpath)
                self.ports += port
            if self.ports:
                self.ready = True

    def sendAlert(self, fedNumber):
        """
        Sends an alert using yagmail that a FED is jammed
        """
        print(f"jam detected on fed {fedNumber}")
        if self.email_enabled:
            try:
                subject = f"FEDWatcher alert for FED{fedNumber}: Jam"
                body = f"Jam detected on FED{fedNumber}"
                self.send_email(subject, body)
                print("Email sent")
            except Exception as e: # catch all exception, otherwise FEDWatcher will halt and stop monitoring
                print(f"Error occurred in sending email {e}")
        if self.tg_enabled:
            self.send_tg_message(message = f"jam detected on fed {fedNumber}")
            
    def sendErrorAlert(self, fedNumber, error_msg):
        """
        Sends an error alert to notify about an error.
        """
        print(f"Error: {error_msg}")
        if self.email_enabled:
            try:
                subject = f"FEDWatcher error alert for FED{fedNumber}"
                body = error_msg
                self.send_email(subject, body)
                print("Email sent")
            except Exception as e:
                print(f"Error occurred in sending email: {e}")
        if self.tg_enabled:
            self.send_tg_message(message=f"Error on FED{fedNumber}: {error_msg}")

    def readPort(self, port, f=None, multi=False, verbose=False, lockInd=None):
        """
        Reads from a serial port until a UTF-8 newline character \n
        arguments:
            port: pointer to the serial port object to read
            f: function to call on read with argument line
            verbose: prints all input if true
        """
        if multi:
          self.main_thread = False
        line = port.readline()
        self.now_dt = datetime.datetime.now()
        if lockInd is not None:
           self.port_locks[lockInd] = False
        line = str(line)[2:-5]

        # Hardcoded jam alert from FED3_Library with software serial enabled, formatted f"{Device_Number},jam"
        # To change this, you must also change the jam message sent in the FED3_Library
        if line[-3:] == "jam":
            self.sendAlert(line[:-4])
            return
        ret = self._format_line_dict(line, self.now_dt)

        # Save to dataframe
        if multi:
            self.data_queue.put(ret)
        else:
            self._frame_update(ret)

        # For calling functions immediately in this thread
        if f is not None:
            f(ret)
        if verbose:
            print(line)

    def runHelper(self, f=None, multi=False, verbose=False):
        """
        Main function helper, should not be called directly
        Loops indefinitely in this thread, reading all serial ports with ~1 ms delay between each loop
        Arguments:
            f: the function to call upon receiving and reading a line from a port with argument of the line
            multi: if true, uses multiprocessing to poll ports faster
            verbose: if true, prints out all lines received
        """
        self.main_thread = True
        for port in self.ports:
            port.reset_input_buffer()

        while True:
            for i, port in enumerate(self.ports):
                if port.inWaiting():
                    if multi:
                       self.port_locks[i] = True
                       mp.Process(target=self.readPort, args=(port, f, multi, verbose, i)).start()
                    else:
                        self.port_locks[i] = True
                        self.readPort(port, f, multi, verbose, i)

            # If in multiprocessing, receive data from the ports through a queue
            if multi:
                while not self.data_queue.empty():
                    ret = self.data_queue.get(block=False)
                    if ret is None: # means queue is blocked, wait til next loop
                        break
                    self._frame_update(ret)

            # Intermittently save the dataframes to csv
            now = time.time()
            if now - self.last_save > self.save_interval:
                self._save_all_df()
                self.last_save = now

            # TODO: replicate this idea but with the notification interval
            self.now_dt = datetime.datetime.now()
            if (self.now_dt - self.last_notif).total_seconds() > self.notif_interval * 3600:
                self._save_all_df()
                self.last_save = now
                # it might be that the folder doesn't exist yet because no data has been saved
                if os.path.exists(self.today_dir):
                    csv_files = [file for file in os.listdir(self.today_dir) if file.endswith('.csv')]
                    today = datetime.date.today()
                    timestr = f"{today.month:02d}" + f"{today.day:02d}" + str(today.year%100)
                    # only report about files that contain today's string
                    csv_files = [file for file in csv_files if timestr in file]
                    #print(csv_files)
                    for fn in csv_files:
                        #print(f"summary for {fn}")
                        summary = self.event_summary(fn)
                        if self.tg_enabled:
                            self.send_tg_message(message=summary)
                        else:
                            print(summary)
                else:
                    summary = f"No events saved before {self.format_human_time(self.now_dt)}"
                    if self.tg_enabled:
                        self.send_tg_message(message=summary)
                    else:
                        print(summary)
                    
                # reset last notif
                self.last_notif = self.now_dt
                

            time.sleep(0.0009)  # loop without reading a port takes about 0.0001, total time ~1ms per loop

    def run(self, f=None, multi=False, verbose=True, configpath=None):
        """
        Main function
        Loops indefinitely in the background, reading all serial ports with ~1 ms delay between each loop
        Arguments:
            f: the function to call upon receiving and reading a line from a port with argument of the line
            multi: if true, uses multiprocessing to poll ports faster, still experimental
            verbose: if true, prints out all lines received
        """
        if not self.ready:
            raise RuntimeError("Ports are not setup")
        if self.running:
            raise RuntimeError("Process is already running")
        self.running = True

        if configpath is not None: 
            self.configpath = configpath
            self.check_config()

        # Checks to make all ports are running. If one is not running, attempts to open it
        for port in self.ports:
            if not port.is_open:
                port.open()

        self.last_save = time.time()
        self.last_notif = datetime.datetime.now()
        self.now_dt = datetime.datetime.now()
        self.run_process = mp.Process(target=self.runHelper, args=(f, multi, verbose))
        self.run_process.start()
        print("FEDWatcher started :)")


    def stop(self):
        """
        Stops all watcher processes and saves dataframes to csv
        """
        if not self.running:
            raise RuntimeError("Process is not running")
        self.run_process.terminate()
        self.running = False


    def close(self):
        """
        Stops running of program and closes all serial ports
        """
        if self.running:
            self.stop()
        self.ready = False
        self.close_ports()


    def close_ports(self):
        """
        Closes all serial ports without stopping process
        """
        for port in self.ports:
            port.close()


    def get_ports(self):
        """
        Returns tuple of active ports
        """
        return self.ports


    def is_ready(self):
        """
        Returns True if set up and ports are open, else false
        """
        return self.ready


    def is_running(self):
        """
        Returns True if running, else false
        """
        return self.running

    
    def check_config(self):
        if self.configpath is not None:
            if os.path.isfile(self.configpath):
                config = configparser.ConfigParser()
                config.read(self.configpath)
                try:
                    self.exp_name = config['fedwatcher']['exp_name']
                except KeyError: 
                    print("config file does not specify experiment name. Using Fedwatcher as experiment name.")
                try:
                    self.exp_dir = config['fedwatcher']['exp_dir']
                except KeyError: 
                    print("config file does not specify save directory. Using Documents as save directory.")
                try:
                    self.session_num = int(config['fedwatcher']['session_num'])
                except KeyError:
                    print("config file does not specify session number. Using 0 as session number.")
                except ValueError:
                    print("config file has an invalid entry for session number")
            else:
                print("No config file found. Using experiment name 'Fedwatcher' in save directory 'Documents' with session number 0.")
        elif self.configpath is None:
            print("No config file found. Using experiment name 'Fedwatcher' in save directory 'Documents' with session number 0.")


    def exit_gracefully(self, *args):
        """
        used for termination of the runHelper function in multiprocessing
        """
        if self.running:
            if self.main_thread:
                print("Terminate received, saving all dataframes and terminating main thread")
                self._save_all_df()
                self.close_ports()
            else:
                print("Terminating non-main thread")
        else:
            print("Inactive fedwatcher terminated")
        sys.exit(0)


    ##
    #  Formatting Functions
    ##

    def _format_line_list(self, line, now):
        l = line.split(",")
        l.insert(0, now)
        return l

    def _format_line_dict(self, line, now):
        l = line.split(",")
        d = {'Pi_Time': now}
        for item, column in zip(l, self.columns[1:]):
            d[column] = item
        return d

    ##
    #  Data saving functions
    ##

    def _save_to_csv(self, df_data):
        if not isinstance(df_data[0]['Device_Number'], int):
            warnings.warn("The 'Device_Number' value is not an integer. Possible Data Corruption! Attempting to convert to int.")
            try:
                device_number = int(float(df_data[0]['Device_Number']))
            except ValueError:
                error_msg = "Unable to convert 'Device_Number' to an integer."
                self.sendErrorAlert(df_data[0]['Device_Number'], error_msg)
                raise ValueError(error_msg)
            warnings.warn("Successfully converted 'Device_Number' to int.")
        else:
            device_number = df_data[0]['Device_Number']

        df = self._new_df(df_data)
        today = datetime.date.today()
        timestr = f"{today.month:02d}" + f"{today.day:02d}" + str(today.year % 100)

        filename = f"FED{device_number:03d}_{timestr}_{self.session_num:02d}.csv"

        self.today_dir = os.path.join(self.exp_dir, str(today.year), f"{today.month:02d}")
        if not os.path.exists(self.today_dir):
            os.makedirs(self.today_dir)
        path = os.path.join(self.today_dir, filename)
        if not os.path.isfile(path):
            df.to_csv(path_or_buf=path, mode='a', index=False)
        else:
            df.to_csv(path_or_buf=path, mode='a', index=False, header=False)

    def _new_df(self, df_data=None):
        return pd.DataFrame(columns=self.columns, data=df_data)

    def _frame_update(self, data):
        """
        Creates/updates dataframes as dictionaries with column headers pointing to list of data in order of oldest to most recent
        """ 
        try:
            Device_Number = data["Device_Number"]
        except KeyError: # invalid data. Catch so fedwatcher does not halt
            return
        if Device_Number not in self.df_dict:
            self.df_dict[Device_Number] = [data,]
        else:
            self.df_dict[Device_Number].append(data)
            if len(self.df_dict[Device_Number]) >= self.max_size:
                self._save_to_csv(self.df_dict[Device_Number])
                self.df_dict[Device_Number] = []

    def _save_all_df(self, reset=True):
        """
        Converts all dataframe dictionaries to pandas dataframes and saves them to csv files
        """
        for df_data in self.df_dict.values():
            self._save_to_csv(df_data)
        if reset:
            self.df_dict = {}

    def get_device_numbers(self):
        """
        Returns a list of the FEDS that currently have data stored in the scripts
        """
        return self.df_dict.keys()

    def get_dataframes(self):
        """
        Returns a list of pandas dataframes of the current data in storage
        """
        l = []
        for df in self.df_dict.values():
            l.append(self._new_df(df))
        return l

    def get_dataframe(self, Device_Number):
        """
        Returns a dataframe of the Fed with device number Device_Number.
        """
        if Device_Number not in self.df_dict:
            return self._new_df()
        else:
            return self._new_df(self.df_dict[Device_Number])

    ###
    #   Telegram Function
    ###
    def find_telegram_keys(self):
        if self.tg_enabled:
            root = tk.Tk()
            root.withdraw()
            file_path = tkinter.filedialog.askopenfilename(title="Choose YAML with Telegram Credentials",
                                                           initialdir = os.path.expanduser('~'),
                                                           filetypes=(("YAML", "*.yaml"), ("All files", "*.*"))
                                                           )
            config = configparser.ConfigParser()
            config.read(file_path)
            self.bot_token = config.get("telegram", "bot_token")
            self.chat_id = config.get("telegram", "chat_id")
            #print(f"Will use bot {self.bot_token} to message {self.chat_id}")
            self.send_tg_message(message = f"FEDWatcher Started {self.format_human_time(datetime.datetime.now())}")
            self.send_tg_message(message = f"Notification frequency set to {self.notif_interval} hours")
            return

    def send_tg_message (self, message):
        # Telegram send message URL
        sendURL = 'https://api.telegram.org/bot' + self.bot_token + '/sendMessage'
        response = requests.post(sendURL + "?chat_id=" + str(self.chat_id) + "&text=" + message)
        # Close to avoid filling up the RAM.
        response.close()

    ###
    #   Summary Functions
    ###
    def format_human_time(self, dt):
        return dt.replace(microsecond=0).isoformat(" ")
    
    def event_summary(self, filename):
        ## want to return message in format "FED{Device #} delivered {num_rows} pellets since {Time}"
        # INPUTS: data, last time interval
        # OUTPUTS: a summarized message in string form

        # iterate through dataframe
        path = os.path.join(self.today_dir, filename)
        df = pd.read_csv(path)
        battery = self.get_battery(df)
        # make the column datetime
        df["Pi_Time"] = pd.to_datetime(df["Pi_Time"])
        device_number = filename.split("_")[0]
        # get the session number.csv
        session_number = filename.split("_")[-1]
        # remove the csv
        session_number = session_number.replace(".csv", "")
        # filter through events using PiTime
        filtered_df = df.loc[(df['Pi_Time'] > self.last_notif) & (df['Pi_Time'] < self.now_dt) & (df['Event']=="Pellet")]
        # pellets = number of rows in df after filtering
        num_pellets = len(filtered_df.index)
        human_time = self.format_human_time(self.last_notif)
        message = f"{device_number}\nsession: {session_number}\nPellets: {num_pellets} since {human_time}\nLast Battery read: {battery}V"
        return message
    
    def get_battery(self, df):
        return df.iloc[[-1]]['Battery_Voltage'].item()

    ###
    #   Mail Function
    ###

    def register_email(self, email, password, store=False):
        """
        When called, enables email alerts, sending from email to themself using yagmail
        Password stored using keyring and can be deleted using delete function
        Args:
            email: gmail account to send from and to
            password: password for gmail, recommend using gmail 2FA with app passwords
            store: boolean for whether to store email password in keychain (to be done later)
        """
        try:
            ## Incomplete: for storing of password
            # if store and password is not None:
            #     yagmail.register(email, password)
            #     self.yag = yagmail.SMTP(email)
            # elif store:
            #     self.yag = yagmail.SMTP(email)
            # elif password is not None:
            #     self.yag = yagmail.SMTP(email, password)
            # else:
            #     print("Password not given")
            #     return False
            self.yag = yagmail.SMTP(email, password)
            self.email_enabled = True
            print("Email enabled successfully")
            return True
        except YagInvalidEmailAddress:
            print("An invalid email address was given")
            return False
        except keyring.errors.KeyringLocked:
            print("keyring is locked, please enter keyring password")
            return False
        except SMTPAuthenticationError as e:
            print(f"Email or password is incorrect {e}")
            return False
        except SMTPServerDisconnected as e:
            print(f"Unable to connect to email {e}")
            return False

    def delete_email(self):
        """
        Must already have an email registered. Deletes the email password
        """
        keyring.delete_password("yagmail", self.email)
        self.email_enabled = False

    def send_email(self, subject, body):
        """
        Sends an email using yagmail. register_email() must be called before this
        Args:
            to: email to be sent to
            subject: subject of the email
            body: message to be sent
        """
        self.yag.send(subject=subject, contents=body)


if __name__ == "__main__":
    try:
        print("Starting fedwatch")
        fw = Fedwatcher()
        fw.run(verbose=True)
        print("started")
        print(f"Running: {fw.is_running()}, Ready: {fw.is_ready()}")
        while True:
            pass
    except KeyboardInterrupt:
        print("stopping and closing fedwatch")
        fw.stop()
        fw.close()
        print("finished")
        print(f"Running: {fw.is_running()}, Ready: {fw.is_ready()}")



