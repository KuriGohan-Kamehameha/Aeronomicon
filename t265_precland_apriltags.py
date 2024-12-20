#!/usr/bin/env python3
import sys
sys.path.append("/usr/local/lib/")

# Remove the path to python2 version that is added by ROS
if '/opt/ros/kinetic/lib/python2.7/dist-packages' in sys.path:
    sys.path.remove('/opt/ros/kinetic/lib/python2.7/dist-packages')

# Set MAVLink protocol to 2
import os
os.environ["MAVLINK20"] = "1"

# Import the libraries
import pyrealsense2.pyrealsense2 as rs
import cv2
import numpy as np
import transformations as tf
import math as m
import time
import argparse
import threading
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from dronekit import connect, VehicleMode
from pymavlink import mavutil

try:
    import apriltags3
except ImportError:
    raise ImportError('Please download the Python wrapper for apriltag3 (apriltags3.py) and put it in the same folder as this script or add the directory path to the PYTHONPATH environment variable.')

#######################################
# Parameters for FCU and MAVLink
#######################################

# Default configurations for connection to the FCU
connection_string_default = '/dev/ttyUSB0'
connection_baudrate_default = 921600
vision_msg_hz_default = 20
landing_target_msg_hz_default = 20
confidence_msg_hz_default = 1
camera_orientation_default = 1

# In NED frame, offset from the IMU or the center of gravity to the camera's origin point
body_offset_enabled = 0
body_offset_x = 0.05    # In meters (m), so 0.05 = 5cm
body_offset_y = 0       # In meters (m)
body_offset_z = 0       # In meters (m)

# Global scale factor, position x y z will be scaled up/down by this factor
scale_factor = 1.0

# Enable using yaw from compass to align north (zero degree is facing north)
compass_enabled = 0

# Default global position of home/origin
home_lat = 151269321       # Somewhere in Africa
home_lon = 16624301        # Somewhere in Africa
home_alt = 163000

# Timestamp (UNIX Epoch time or time since system boot)
current_time = 0

vehicle = None
pipe = None

# Pose data confidence: 0x0 - Failed / 0x1 - Low / 0x2 - Medium / 0x3 - High
pose_data_confidence_level = ('Failed', 'Low', 'Medium', 'High')

#######################################
# Parsing user's inputs
#######################################

parser = argparse.ArgumentParser(description='ArduPilot + Realsense T265 + AprilTags')
parser.add_argument('--connect',
                    help="Vehicle connection target string",
                    default=connection_string_default)
parser.add_argument('--baudrate', type=int,
                    help="Vehicle connection baudrate",
                    default=connection_baudrate_default)
parser.add_argument('--vision_msg_hz', type=float,
                    help="Update frequency for VISION_POSITION_ESTIMATE message",
                    default=vision_msg_hz_default)
parser.add_argument('--landing_target_msg_hz', type=float,
                    help="Update frequency for LANDING_TARGET message",
                    default=landing_target_msg_hz_default)
parser.add_argument('--confidence_msg_hz', type=float,
                    help="Update frequency for confidence level",
                    default=confidence_msg_hz_default)
parser.add_argument('--camera_orientation', type=int,
                    help="Configuration for camera orientation. Currently supported: forward, USB port to the right - 0; downward, USB port to the right - 1",
                    default=camera_orientation_default)
parser.add_argument('--visualization', action='store_true',
                    help="Enable visualization. Ensure that a monitor is connected")
parser.add_argument('--debug_enable', action='store_true',
                    help="Enable debug messages on terminal")
parser.add_argument('--scale_calib_enable', action='store_true',
                    help="Enable scale calibration. Only run while NOT in flight")

args = parser.parse_args()

connection_string = args.connect
connection_baudrate = args.baudrate
vision_msg_hz = args.vision_msg_hz
landing_target_msg_hz = args.landing_target_msg_hz
confidence_msg_hz = args.confidence_msg_hz
scale_calib_enable = args.scale_calib_enable
camera_orientation = args.camera_orientation
visualization = args.visualization
debug_enable = args.debug_enable

# Configure logging
if debug_enable:
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(message)s')
    np.set_printoptions(precision=4, suppress=True)
    logging.debug("Debug messages enabled.")
else:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

logging.info(f"Using connection_string: {connection_string}")
logging.info(f"Using connection_baudrate: {connection_baudrate}")
logging.info(f"Using vision_msg_hz: {vision_msg_hz}")
logging.info(f"Using landing_target_msg_hz: {landing_target_msg_hz}")
logging.info(f"Using confidence_msg_hz: {confidence_msg_hz}")

if body_offset_enabled == 1:
    logging.info(f"Using camera position offset: Enabled, x y z is {body_offset_x} {body_offset_y} {body_offset_z}")
else:
    logging.info("Using camera position offset: Disabled")

if compass_enabled == 1:
    logging.info("Using compass: Enabled. Heading will be aligned to north.")
else:
    logging.info("Using compass: Disabled")

if scale_calib_enable:
    logging.info("\nSCALE CALIBRATION PROCESS. DO NOT RUN DURING FLIGHT.\nTYPE IN NEW SCALE IN FLOATING POINT FORMAT\n")
else:
    if scale_factor == 1.0:
        logging.info(f"Using default scale factor: {scale_factor}")
    else:
        logging.info(f"Using scale factor: {scale_factor}")

if visualization:
    logging.info("Visualization: Enabled. Checking if monitor is connected...")
    WINDOW_TITLE = 'Apriltag detection from T265 images'
    cv2.namedWindow(WINDOW_TITLE, cv2.WINDOW_AUTOSIZE)
    logging.info("Monitor is connected. Press `q` to exit.")
    display_mode = "stack"
else:
    logging.info("Visualization: Disabled")

# Transformation to convert different camera orientations to NED convention
if camera_orientation == 0:
    # Forward, USB port to the right
    H_aeroRef_T265Ref = np.array([[0, 0, -1, 0],
                                  [1, 0,  0, 0],
                                  [0, -1, 0, 0],
                                  [0,  0,  0, 1]])
    H_T265body_aeroBody = np.linalg.inv(H_aeroRef_T265Ref)
elif camera_orientation == 1:
    # Downfacing, USB port to the right
    H_aeroRef_T265Ref = np.array([[0, 0, -1, 0],
                                  [1, 0,  0, 0],
                                  [0, -1, 0, 0],
                                  [0,  0,  0, 1]])
    H_T265body_aeroBody = np.array([[0, 1,  0, 0],
                                    [1, 0,  0, 0],
                                    [0, 0, -1, 0],
                                    [0, 0,  0, 1]])
else:
    logging.error(f"Unsupported camera orientation: {camera_orientation}")
    sys.exit(1)

#######################################
# Functions for OpenCV
#######################################

def get_extrinsics(src, dst):
    extrinsics = src.get_extrinsics_to(dst)
    R = np.reshape(extrinsics.rotation, [3, 3]).T
    T = np.array(extrinsics.translation)
    return (R, T)

def camera_matrix(intrinsics):
    return np.array([[intrinsics.fx, 0, intrinsics.ppx],
                     [0, intrinsics.fy, intrinsics.ppy],
                     [0, 0, 1]])

def fisheye_distortion(intrinsics):
    return np.array(intrinsics.coeffs[:4])

#######################################
# Functions for AprilTag detection
#######################################
tag_landing_id = 0
tag_landing_size = 0.144  # Tag's border size, measured in meters
tag_image_source = "right"  # For Realsense T265, we can use "left" or "right"

if tag_image_source not in ["left", "right"]:
    logging.error(f"Invalid tag_image_source: {tag_image_source}")
    sys.exit(1)

at_detector = apriltags3.Detector(searchpath=['apriltags'],
                                  families='tag36h11',
                                  nthreads=1,
                                  quad_decimate=1.0,
                                  quad_sigma=0.0,
                                  refine_edges=1,
                                  decode_sharpening=0.25,
                                  debug=0)

#######################################
# Functions for MAVLink
#######################################

def send_land_target_message():
    global current_time, H_camera_tag, is_landing_tag_detected

    if is_landing_tag_detected and H_camera_tag is not None:
        x = H_camera_tag[0][3]
        y = H_camera_tag[1][3]
        z = H_camera_tag[2][3]

        if z == 0:
            logging.warning("Z distance is zero, cannot compute offsets.")
            return

        x_offset_rad = m.atan2(x, z)
        y_offset_rad = m.atan2(y, z)
        distance = np.sqrt(x * x + y * y + z * z)

        try:
            msg = vehicle.message_factory.landing_target_encode(
                current_time,                       # time target data was processed, as close to sensor capture as possible
                0,                                  # target num, not used
                mavutil.mavlink.MAV_FRAME_BODY_NED, # frame, not used
                x_offset_rad,                       # X-axis angular offset, in radians
                y_offset_rad,                       # Y-axis angular offset, in radians
                distance,                           # distance, in meters
                0,                                  # Target x-axis size, in radians
                0,                                  # Target y-axis size, in radians
                0,                                  # x position (not used)
                0,                                  # y position (not used)
                0,                                  # z position (not used)
                (1, 0, 0, 0),                       # Quaternion of landing target orientation (w, x, y, z)
                2,                                  # Type of landing target: 2 = Fiducial marker
                1                                   # Position_valid boolean
            )
            vehicle.send_mavlink(msg)
            vehicle.flush()
        except Exception as e:
            logging.error(f"Error sending landing_target message: {e}")

def send_vision_position_message():
    global current_time, H_aeroRef_aeroBody

    if H_aeroRef_aeroBody is not None:
        rpy_rad = np.array(tf.euler_from_matrix(H_aeroRef_aeroBody, 'sxyz'))
        pos = tf.translation_from_matrix(H_aeroRef_aeroBody)

        try:
            msg = vehicle.message_factory.vision_position_estimate_encode(
                current_time,      # Timestamp (UNIX time or time since system boot)
                pos[0],            # Global X position
                pos[1],            # Global Y position
                pos[2],            # Global Z position
                rpy_rad[0],        # Roll angle
                rpy_rad[1],        # Pitch angle
                rpy_rad[2]         # Yaw angle
            )
            vehicle.send_mavlink(msg)
            vehicle.flush()
        except Exception as e:
            logging.error(f"Error sending vision_position_estimate message: {e}")

def send_confidence_level_dummy_message():
    global data, current_confidence
    if data is not None:
        if 0 <= data.tracker_confidence <= 3:
            confidence_str = pose_data_confidence_level[data.tracker_confidence]
            logging.info(f"Tracking confidence: {confidence_str}")
            confidence_percent = float(data.tracker_confidence * 100 / 3)

            try:
                msg = vehicle.message_factory.vision_position_delta_encode(
                    0,          # Timestamp (unused)
                    0,          # Time since last frame (unused)
                    [0, 0, 0],  # Angle delta (unused)
                    [0, 0, 0],  # Position delta (unused)
                    confidence_percent
                )
                vehicle.send_mavlink(msg)
                vehicle.flush()

                if current_confidence is None or current_confidence != data.tracker_confidence:
                    current_confidence = data.tracker_confidence
                    confidence_status_string = 'Tracking confidence: ' + confidence_str
                    status_msg = vehicle.message_factory.statustext_encode(
                        mavutil.mavlink.MAV_SEVERITY_INFO,
                        confidence_status_string.encode()
                    )
                    vehicle.send_mavlink(status_msg)
                    vehicle.flush()
            except Exception as e:
                logging.error(f"Error sending confidence level message: {e}")
        else:
            logging.warning(f"Invalid tracker confidence value: {data.tracker_confidence}")

def set_default_global_origin():
    try:
        msg = vehicle.message_factory.set_gps_global_origin_encode(
            int(vehicle._master.source_system),
            home_lat,
            home_lon,
            home_alt
        )
        vehicle.send_mavlink(msg)
        vehicle.flush()
    except Exception as e:
        logging.error(f"Error setting default global origin: {e}")

def set_default_home_position():
    try:
        x = 0
        y = 0
        z = 0
        q = [1, 0, 0, 0]  # w x y z

        approach_x = 0
        approach_y = 0
        approach_z = 1

        msg = vehicle.message_factory.set_home_position_encode(
            int(vehicle._master.source_system),
            home_lat,
            home_lon,
            home_alt,
            x,
            y,
            z,
            q,
            approach_x,
            approach_y,
            approach_z
        )
        vehicle.send_mavlink(msg)
        vehicle.flush()
    except Exception as e:
        logging.error(f"Error setting default home position: {e}")

def statustext_callback(self, attr_name, value):
    if value.text in ["GPS Glitch", "GPS Glitch cleared", "EKF2 IMU1 ext nav yaw alignment complete"]:
        time.sleep(0.1)
        logging.info("Set EKF home with default GPS location")
        set_default_global_origin()
        set_default_home_position()

def att_msg_callback(self, attr_name, value):
    global heading_north_yaw
    if heading_north_yaw is None:
        heading_north_yaw = value.yaw
        logging.info(f"Received first ATTITUDE message with heading yaw {heading_north_yaw * 180 / m.pi} degrees")
    else:
        heading_north_yaw = value.yaw
        logging.debug(f"Updated heading yaw: {heading_north_yaw * 180 / m.pi} degrees")

def scale_update():
    global scale_factor
    while True:
        try:
            user_input = input("Type in new scale as float number\n")
            with frame_mutex:
                scale_factor = float(user_input)
            logging.info(f"New scale is {scale_factor}")
        except ValueError:
            logging.error("Invalid input. Please enter a floating point number.")

def vehicle_connect():
    global vehicle
    try:
        vehicle = connect(connection_string, wait_ready=True, baud=connection_baudrate, source_system=1)
        return True
    except KeyboardInterrupt:
        logging.info("Exiting due to KeyboardInterrupt.")
        sys.exit()
    except Exception as e:
        logging.error(f"Connection error: {e}")
        return False

def realsense_connect():
    global pipe
    try:
        pipe = rs.pipeline()
        cfg = rs.config()
        cfg.enable_stream(rs.stream.pose)
        cfg.enable_stream(rs.stream.fisheye, 1)
        cfg.enable_stream(rs.stream.fisheye, 2)
        pipe.start(cfg)
    except Exception as e:
        logging.error(f"Realsense connection error: {e}")
        sys.exit(1)

#######################################
# Main code starts here
#######################################

frame_mutex = threading.Lock()

logging.info("Connecting to Realsense camera.")
realsense_connect()
logging.info("Realsense connected.")

logging.info("Connecting to vehicle.")
while not vehicle_connect():
    time.sleep(1)
logging.info("Vehicle connected.")

vehicle.add_message_listener('STATUSTEXT', statustext_callback)

if compass_enabled == 1:
    vehicle.add_message_listener('ATTITUDE', att_msg_callback)

data = None
current_confidence = None
H_aeroRef_aeroBody = None
H_camera_tag = None
is_landing_tag_detected = False
heading_north_yaw = None

# Send MAVLink messages in the background
sched = BackgroundScheduler()
sched.add_job(send_vision_position_message, 'interval', seconds=1/vision_msg_hz)
sched.add_job(send_confidence_level_dummy_message, 'interval', seconds=1/confidence_msg_hz)
sched.add_job(send_land_target_message, 'interval', seconds=1/landing_target_msg_hz)

if scale_calib_enable:
    scale_update_thread = threading.Thread(target=scale_update)
    scale_update_thread.daemon = True
    scale_update_thread.start()

sched.start()

if compass_enabled == 1:
    time.sleep(1)

logging.info("Starting main loop...")

try:
    # Configure the OpenCV stereo algorithm
    window_size = 5
    min_disp = 16
    num_disp = 112 - min_disp
    max_disp = min_disp + num_disp
    stereo = cv2.StereoSGBM_create(minDisparity=min_disp,
                                   numDisparities=num_disp,
                                   blockSize=16,
                                   P1=8 * 3 * window_size ** 2,
                                   P2=32 * 3 * window_size ** 2,
                                   disp12MaxDiff=1,
                                   uniquenessRatio=10,
                                   speckleWindowSize=100,
                                   speckleRange=32)

    # Retrieve the stream and intrinsic properties for both cameras
    profiles = pipe.get_active_profile()
    streams = {"left": profiles.get_stream(rs.stream.fisheye, 1).as_video_stream_profile(),
               "right": profiles.get_stream(rs.stream.fisheye, 2).as_video_stream_profile()}
    intrinsics = {"left": streams["left"].get_intrinsics(),
                  "right": streams["right"].get_intrinsics()}

    logging.info("Using stereo fisheye cameras")
    if debug_enable:
        logging.debug(f"T265 Left camera: {intrinsics['left']}")
        logging.debug(f"T265 Right camera: {intrinsics['right']}")

    # Translate the intrinsics from librealsense into OpenCV
    K_left = camera_matrix(intrinsics["left"])
    D_left = fisheye_distortion(intrinsics["left"])
    K_right = camera_matrix(intrinsics["right"])
    D_right = fisheye_distortion(intrinsics["right"])
    (width, height) = (intrinsics["left"].width, intrinsics["left"].height)

    # Get the relative extrinsics between the left and right camera
    (R, T) = get_extrinsics(streams["left"], streams["right"])

    # Calculate the undistorted focal length
    stereo_fov_rad = 90 * (m.pi / 180)
    stereo_height_px = 300
    stereo_focal_px = stereo_height_px / 2 / m.tan(stereo_fov_rad / 2)

    R_left = np.eye(3)
    R_right = R

    stereo_width_px = stereo_height_px + max_disp
    stereo_size = (stereo_width_px, stereo_height_px)
    stereo_cx = (stereo_height_px - 1) / 2 + max_disp
    stereo_cy = (stereo_height_px - 1) / 2

    P_left = np.array([[stereo_focal_px, 0, stereo_cx, 0],
                       [0, stereo_focal_px, stereo_cy, 0],
                       [0, 0, 1, 0]])
    P_right = P_left.copy()
    P_right[0][3] = T[0] * stereo_focal_px

    Q = np.array([[1, 0, 0, -(stereo_cx - max_disp)],
                  [0, 1, 0, -stereo_cy],
                  [0, 0, 0, stereo_focal_px],
                  [0, 0, -1 / T[0], 0]])

    # Create an undistortion map for the left and right camera
    m1type = cv2.CV_32FC1
    (lm1, lm2) = cv2.fisheye.initUndistortRectifyMap(
        K_left, D_left, R_left, P_left, stereo_size, m1type)
    (rm1, rm2) = cv2.fisheye.initUndistortRectifyMap(
        K_right, D_right, R_right, P_right, stereo_size, m1type)
    undistort_rectify = {"left": (lm1, lm2),
                         "right": (rm1, rm2)}

    # For AprilTag detection
    camera_params = [stereo_focal_px, stereo_focal_px, stereo_cx, stereo_cy]

    while True:
        try:
            # Wait for the next set of frames from the camera
            frames = pipe.wait_for_frames()
        except Exception as e:
            logging.error(f"Error waiting for frames: {e}")
            continue

        # Fetch pose frame
        pose = frames.get_pose_frame()

        # Process pose streams
        if pose:
            current_time = int(round(time.time() * 1000000))
            data = pose.get_pose_data()

            H_T265Ref_T265body = tf.quaternion_matrix(
                [data.rotation.w, data.rotation.x, data.rotation.y, data.rotation.z])
            with frame_mutex:
                H_T265Ref_T265body[0][3] = data.translation.x * scale_factor
                H_T265Ref_T265body[1][3] = data.translation.y * scale_factor
                H_T265Ref_T265body[2][3] = data.translation.z * scale_factor

            H_aeroRef_aeroBody = H_aeroRef_T265Ref.dot(
                H_T265Ref_T265body.dot(H_T265body_aeroBody))

            # Take offsets from body's center of gravity (or IMU) to camera's origin into account
            if body_offset_enabled == 1:
                H_body_camera = tf.euler_matrix(0, 0, 0, 'sxyz')
                H_body_camera[0][3] = body_offset_x
                H_body_camera[1][3] = body_offset_y
                H_body_camera[2][3] = body_offset_z
                H_camera_body = np.linalg.inv(H_body_camera)
                H_aeroRef_aeroBody = H_body_camera.dot(
                    H_aeroRef_aeroBody.dot(H_camera_body))

            # Realign heading to face north using initial compass data
            if compass_enabled == 1 and heading_north_yaw is not None:
                H_aeroRef_aeroBody = H_aeroRef_aeroBody.dot(
                    tf.euler_matrix(0, 0, heading_north_yaw, 'sxyz'))

            # Show debug messages here
            if debug_enable:
                os.system('clear')
                logging.debug(f"Raw RPY[deg]: {np.array(tf.euler_from_matrix(H_T265Ref_T265body, 'sxyz')) * 180 / m.pi}")
                logging.debug(f"NED RPY[deg]: {np.array(tf.euler_from_matrix(H_aeroRef_aeroBody, 'sxyz')) * 180 / m.pi}")
                logging.debug(f"Raw pos xyz: {np.array([data.translation.x, data.translation.y, data.translation.z])}")
                logging.debug(f"NED pos xyz: {np.array(tf.translation_from_matrix(H_aeroRef_aeroBody))}")

        # Fetch raw fisheye image frames
        try:
            f1 = frames.get_fisheye_frame(1).as_video_frame()
            left_data = np.asanyarray(f1.get_data())
            f2 = frames.get_fisheye_frame(2).as_video_frame()
            right_data = np.asanyarray(f2.get_data())
        except Exception as e:
            logging.error(f"Error fetching fisheye frames: {e}")
            continue

        # Process image streams
        frame_copy = {"left": left_data, "right": right_data}

        try:
            # Undistort and crop the center of the frames
            center_undistorted = {
                "left": cv2.remap(src=frame_copy["left"],
                                  map1=undistort_rectify["left"][0],
                                  map2=undistort_rectify["left"][1],
                                  interpolation=cv2.INTER_LINEAR),
                "right": cv2.remap(src=frame_copy["right"],
                                   map1=undistort_rectify["right"][0],
                                   map2=undistort_rectify["right"][1],
                                   interpolation=cv2.INTER_LINEAR)}
        except Exception as e:
            logging.error(f"Error during image undistortion: {e}")
            continue

        # Run AprilTag detection algorithm on rectified image
        try:
            tags = at_detector.detect(center_undistorted[tag_image_source],
                                      True, camera_params, tag_landing_size)
        except Exception as e:
            logging.error(f"Error during AprilTag detection: {e}")
            continue

        if tags:
            for tag in tags:
                if tag.tag_id == tag_landing_id:
                    is_landing_tag_detected = True
                    H_camera_tag = tf.euler_matrix(0, 0, 0, 'sxyz')
                    H_camera_tag[0][3] = tag.pose_t[0]
                    H_camera_tag[1][3] = tag.pose_t[1]
                    H_camera_tag[2][3] = tag.pose_t[2]
                    logging.info(f"Detected landing tag {tag.tag_id} relative to camera at x: {H_camera_tag[0][3]}, y: {H_camera_tag[1][3]}, z: {H_camera_tag[2][3]}")
        else:
            is_landing_tag_detected = False

        # Visualization
        if visualization:
            tags_img = center_undistorted[tag_image_source].copy()

            for tag in tags:
                for idx in range(len(tag.corners)):
                    cv2.line(tags_img,
                             tuple(tag.corners[idx - 1, :].astype(int)),
                             tuple(tag.corners[idx, :].astype(int)),
                             thickness=2,
                             color=(255, 0, 0))

                text = str(tag.tag_id)
                textsize = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1, 2)[0]
                cv2.putText(tags_img,
                            text,
                            org=(((tag.corners[0, 0] + tag.corners[2, 0] - textsize[0]) / 2).astype(int),
                                 ((tag.corners[0, 1] + tag.corners[2, 1] + textsize[1]) / 2).astype(int)),
                            fontFace=cv2.FONT_HERSHEY_SIMPLEX,
                            fontScale=0.5,
                            thickness=2,
                            color=(255, 0, 0))

            cv2.imshow(WINDOW_TITLE, tags_img)
            key = cv2.waitKey(1)
            if key == ord('q') or cv2.getWindowProperty(WINDOW_TITLE, cv2.WND_PROP_VISIBLE) < 1:
                break

except KeyboardInterrupt:
    logging.info("KeyboardInterrupt has been caught. Cleaning up...")

finally:
    if pipe is not None:
        pipe.stop()
    if vehicle is not None:
        vehicle.close()
    logging.info("Realsense pipeline and vehicle object closed.")
    sys.exit()
