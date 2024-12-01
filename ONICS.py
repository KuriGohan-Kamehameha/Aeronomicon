#!/usr/bin/env python3

##################################################################
##  ONICS - Optical Navigation and Interference Control System  ##
##################################################################

# This script uses single-core threading to manage connections. 
# ONICS has been rewritten with multiprocessing and released as ONICS 2.  
# If your device is lagging/freezing/hanging/crashing under load, please consider switching. 


import subprocess
import threading
import logging
import time
import os
import argparse
import multiprocessing


# Default log file path
log_file_path = "/home/pi/onics.log"

# Configure logging
logging.basicConfig(filename=log_file_path, level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Define connection parameters
connection_in_port = "127.0.0.1:14550"
connection_in_baud = "921600"
connection_out_p01 = "127.0.0.1:14540"  # T265
connection_out_p02 = "127.0.0.1:14560"  # D435i
connection_out_p03 = "/dev/usb-FTDI_FT230X_Basic_UART_D30AAUZG-if00-port0"     # SiK Radio

def is_device_connected(device_prefix):
    try:
        result = subprocess.run(["rs-enumerate-devices"], capture_output=True, text=True, check=True)
        return any(device_prefix in line for line in result.stdout.splitlines())
    except subprocess.SubprocessError as e:
        logging.error("Error checking for RealSense devices: %s", e)
        return False

def enumerate_devices():
    relevant_devices = ["T265", "D4"]
    try:
        result = subprocess.run(["rs-enumerate-devices"], capture_output=True, text=True, check=True)
        detected_devices = [line.strip() for line in result.stdout.splitlines() if any(dev in line for dev in relevant_devices)]
        if detected_devices:
            print("Detected RealSense Devices:")
            for device in detected_devices:
                print(device)
        else:
            print("No relevant RealSense devices detected.")
    except subprocess.SubprocessError as e:
        logging.error("Error enumerating RealSense devices: %s", e)

def mavproxy_create_connection():
    try:
        subprocess.run(["mavproxy.py",
                        f"--master={connection_in_port}",
                        f"--baudrate={connection_in_baud}",
                        "--out", f"udp:{connection_out_p01}",
                        "--out", f"udp:{connection_out_p02}",
                        "--out", connection_out_p03], check=True)
    except subprocess.SubprocessError as e:
        logging.error("mavproxy error: %s", e)

def run_d4xx():
    while True:
        if is_device_connected("D4"):
            try:
                subprocess.run(["python3", "d4xx_to_mavlink.py", f"--connect={connection_out_p02}"], check=True)
            except subprocess.SubprocessError as e:
                logging.error("D4XX script error: %s", e)
                break
        else:
            logging.warning("No D4XX device detected. Skipping D4XX script.")
            break
        time.sleep(1)

def run_t265():
    while True:
        if is_device_connected("T265"):
            try:
                subprocess.run(["python3", "t265_precland_apriltags.py", f"--connect={connection_out_p01}"], check=True)
            except subprocess.SubprocessError as e:
                logging.error("T265 script error: %s", e)
                break
        else:
            logging.warning("T265 device not detected. Skipping T265 script.")
            break
        time.sleep(1)

def publish_logs():
    with open(log_file_path, 'r') as logfile:
        logfile.seek(0, os.SEEK_END)
        while True:
            line = logfile.readline()
            if line:
                print(line.strip())
            else:
                time.sleep(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ONICS - Optical Navigation and Interference Control System")
    parser.add_argument("-e", "--enumerate", action="store_true", help="Enumerate detected RealSense devices")
    parser.add_argument("-d", "--disable-t265", action="store_true", help="Disable the T265 thread")
    parser.add_argument("-t", "--disable-d4xx", action="store_true", help="Disable the D4XX thread")
    parser.add_argument("-s", "--sik-only", action="store_true", help="Initialize only the SiK radio (disables both T265 and D4XX)")

    args = parser.parse_args()

    if args.enumerate:
        enumerate_devices()
    else:
        t265_enabled = not (args.disable_t265 or args.sik_only)
        d4xx_enabled = not (args.disable_d4xx or args.sik_only)

        log_processing = multiprocessing.Process(target=publish_logs, daemon=True)
        log_processing.start()

        print("ONICS - Optical Navigation and Interference Control System is initializing...")

        processes = [multiprocessing.Process(target=mavproxy_create_connection)]
        if t265_enabled:
            processes.append(multiprocessing.Process(target=run_t265))
        if d4xx_enabled:
            processes.append(multiprocessing.Process(target=run_d4xx))

        for process in processes:
            process.run() 

        for process in processes:
            process.join()  # Wait for threads to complete or terminate
