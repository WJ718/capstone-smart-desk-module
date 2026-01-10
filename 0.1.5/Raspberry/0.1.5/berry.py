import RPi.GPIO as GPIO
import time
import cv2
import dlib
from scipy.spatial import distance as dist
import os
import threading
import serial
import websocket
import json
import subprocess

# GPIO 설정
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

BUZZER_PIN = 18
RELAY_PIN = 12
MOSFET_PIN = 23
DC_MOTOR_PIN_1 = 30
DC_MOTOR_PIN_2 = 27
DC_MOTOR_PWM_PIN = 22
enable_buzzer = False
brightness = 50

GPIO.setup(BUZZER_PIN, GPIO.OUT)
GPIO.setup(MOSFET_PIN, GPIO.OUT)
GPIO.setup(RELAY_PIN, GPIO.OUT)
GPIO.setup(DC_MOTOR_PIN_1, GPIO.OUT)
GPIO.setup(DC_MOTOR_PIN_2, GPIO.OUT)
GPIO.setup(DC_MOTOR_PWM_PIN, GPIO.OUT)

pwm = GPIO.PWM(BUZZER_PIN, 1000)
led_pwm = GPIO.PWM(MOSFET_PIN, 1000)
motor_pwm = GPIO.PWM(DC_MOTOR_PWM_PIN, 1000)
motor_pwm.start(0)

EYE_AR_THRESH = 0.25
ALERT_DURATION = 4
ALERT_INTERVAL = 5
ALERT_COOLDOWN = 10
last_alert_time = 0
close_start_time = None
EAR_FILE = "ear_threshold.txt"
sleep_detection_thread = None
motor_thread = None
stop_sleep_detection = threading.Event()
stop_motor_monitoring = threading.Event()
global ws_world

if not os.path.exists("shape_predictor_68_face_landmarks.dat"):
    raise FileNotFoundError("shape_predictor_68_face_landmarks.dat 파일이 없습니다.")


def get_serial_number():
    try:
        serial = subprocess.check_output("cat /proc/cpuinfo | grep Serial | awk '{print $3}'", shell=True)
        return serial.decode('utf-8').strip()
    except Exception as e:
        print(f"시리얼 넘버 가져오기 실패: {e}")
        return "UNKNOWN"


def calculate_EAR(eye):
    A = dist.euclidean(eye[1], eye[5])
    B = dist.euclidean(eye[2], eye[4])
    C = dist.euclidean(eye[0], eye[3])
    return (A + B) / (2.0 * C)


def alert():
    if enable_buzzer:
        pwm.start(50)
    led_pwm.start(brightness)
    time.sleep(ALERT_INTERVAL)
    if enable_buzzer:
        pwm.stop()
    led_pwm.stop()
    GPIO.output(RELAY_PIN, GPIO.LOW)


try:
    ser = serial.Serial(port='/dev/serial0', baudrate=9600, timeout=1)
except Exception as e:
    print(f"CO2 센서 초기화 오류: {e}")
    ser = None


def load_or_calibrate_ear():
    global EYE_AR_THRESH
    if os.path.exists(EAR_FILE):
        try:
            with open(EAR_FILE, "r") as f:
                EYE_AR_THRESH = float(f.read().strip())
                print(f"[설정 불러옴] EAR 기준값: {EYE_AR_THRESH:.2f}")
        except Exception as e:
            print(f"[오류] EAR 파일 읽기 실패: {e} → 재측정 시작")
            calibrate_ear()
    else:
        calibrate_ear()


def read_co2():
    if ser:
        ser.write(b'\xff\x01\x86\x00\x00\x00\x00\x00\x79')
        time.sleep(0.1)
        response = ser.read(9)
        if len(response) == 9 and response[0] == 0xff and response[1] == 0x86:
            co2 = response[2] * 256 + response[3]
            return co2
    return None


def motor_control():
    motor_active = False
    try:
        while not stop_motor_monitoring.is_set():
            co2 = read_co2()
            if co2 is not None:
                print(f"[CO2 모니터링] 현재 CO2 농도: {co2} ppm")
                if co2 >= 2000 and not motor_active:
                    time.sleep(10)
                    send_co2alert(co2)
                    motor_active = True
                elif co2 < 2000 and motor_active:
                    time.sleep(10)
                    motor_active = False
            else:
                print("[CO2 모니터링] 데이터를 읽을 수 없습니다.")
            time.sleep(2)
    finally:
        GPIO.output(DC_MOTOR_PIN_1, GPIO.LOW)
        GPIO.output(DC_MOTOR_PIN_2, GPIO.LOW)
        motor_pwm.ChangeDutyCycle(0)
        print("[CO2 모니터링] 종료됨")


def calibrate_ear():
    print("[시작] EAR 임계치 설정 중...")
    ear_values = []
    cap = cv2.VideoCapture(0)
    hog_face_detector = dlib.get_frontal_face_detector()
    dlib_facelandmark = dlib.shape_predictor("shape_predictor_68_face_landmarks.dat")

    try:
        while len(ear_values) < 300:
            ret, frame = cap.read()
            if not ret or frame is None or frame.size == 0:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = hog_face_detector(gray)
            for face in faces:
                landmarks = dlib_facelandmark(gray, face)
                leftEye = [(landmarks.part(n).x, landmarks.part(n).y) for n in range(36, 42)]
                rightEye = [(landmarks.part(n).x, landmarks.part(n).y) for n in range(42, 48)]
                EAR = (calculate_EAR(leftEye) + calculate_EAR(rightEye)) / 2
                ear_values.append(EAR)
                print(f"[EAR 캘리브레이션] EAR: {EAR:.2f}")
            if cv2.waitKey(1) & 0xFF == 27:
                raise Exception("중단됨")

        open_avg = sum(sorted(ear_values)[-10:]) / 10
        close_avg = sum(sorted(ear_values)[:10]) / 10
        global EYE_AR_THRESH
        EYE_AR_THRESH = (open_avg + close_avg) / 2
        print(f"[완료] EAR 기준값 설정됨: {EYE_AR_THRESH:.2f}")

        with open(EAR_FILE, "w") as f:
            f.write(str(EYE_AR_THRESH))
        print(f"[저장 완료] EAR 기준값을 {EAR_FILE}에 저장했습니다.")

    except Exception as e:
        print(e)
        cap.release()
        cv2.destroyAllWindows()
        GPIO.cleanup()
        exit()
    finally:
        cap.release()
        cv2.destroyAllWindows()


def detect_sleeping_driver():
    print("[시작] 졸음 감지 모니터링 중...")
    global close_start_time
    global last_alert_time
    global sleep_detection_thread

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[오류] 웹캠 열기 실패")
        return

    hog_face_detector = dlib.get_frontal_face_detector()
    dlib_facelandmark = dlib.shape_predictor("shape_predictor_68_face_landmarks.dat")

    try:
        while not stop_sleep_detection.is_set():
            ret, frame = cap.read()
            if not ret or frame is None or frame.size == 0:
                print("[경고] 프레임 오류")
                if stop_sleep_detection.is_set():
                    break
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = hog_face_detector(gray)
            print(f"[DEBUG] 얼굴 감지 수: {len(faces)}")

            for face in faces:
                landmarks = dlib_facelandmark(gray, face)
                leftEye = [(landmarks.part(n).x, landmarks.part(n).y) for n in range(36, 42)]
                rightEye = [(landmarks.part(n).x, landmarks.part(n).y) for n in range(42, 48)]
                EAR = (calculate_EAR(leftEye) + calculate_EAR(rightEye)) / 2

                current_time = time.time()
                if EAR < EYE_AR_THRESH:
                    if close_start_time is None:
                        close_start_time = current_time
                    elif (
                        current_time - close_start_time >= ALERT_DURATION and
                        current_time - last_alert_time >= ALERT_COOLDOWN
                    ):
                        print("[경고] 졸음 감지! 경고 발생")
                        alert()
                        send_sleepingalert("졸음이 감지되었습니다")
                        last_alert_time = current_time
                else:
                    close_start_time = None

                print(f"[EAR 모니터링] EAR: {EAR:.2f}")

            if stop_sleep_detection.is_set():
                break

    except Exception as e:
        print(f"[오류] 감지 중 예외 발생: {e}")

    finally:
        print("[종료] 졸음 감지 쓰레드 정리 중")
        close_start_time = None
        sleep_detection_thread = None
        cap.release()
        cap = None
        cv2.destroyAllWindows()


def send_co2alert(co2_value):
    if not ws_world:
        print("[WebSocket] 연결 없음 - CO2 알림 전송 불가")
    else:
        try:
            material = {
                'type': 'co2',
                'message': f"CO2 농도가 {co2_value}ppm으로 기준치를 초과했습니다.",
                'timeline': time.time(),
                'serial': get_serial_number()
            }
            ws_world.send(json.dumps(material))
            print("[WebSocket] CO2 알림 전송 완료")
        except Exception as e:
            print(f"[WebSocket 오류] CO2 전송 실패: {e}")


def send_sleepingalert(alert_sign):
    if not ws_world:
        print("[WebSocket] 연결 없음 - 알림 전송 불가")
    else:
        try:
            material = {
                'type': 'sleepy',
                'message': alert_sign,
                'timeline': time.time(),
                'serial': get_serial_number()
            }
            ws_world.send(json.dumps(material))
            print("[WebSocket] 졸음 알림 전송 완료")
        except Exception as e:
            print(f"[WebSocket 오류] 전송 실패: {e}")


def on_message(ws, message):
    global sleep_detection_thread
    global motor_thread
    try:
        data = json.loads(message)

        if data.get("command") == "start":
            print("[WebSocket] 서버로부터 start 명령 수신 → 감지 시작")

            stop_sleep_detection.clear()
            stop_motor_monitoring.clear()

            if sleep_detection_thread is None or not sleep_detection_thread.is_alive():
                load_or_calibrate_ear()
                sleep_detection_thread = threading.Thread(target=detect_sleeping_driver)
                sleep_detection_thread.start()
            else:
                print("[졸음 감지] 이미 실행 중입니다.")

            if motor_thread is None or not motor_thread.is_alive():
                motor_thread = threading.Thread(target=motor_control)
                motor_thread.start()
            else:
                print("[CO2 모니터링] 이미 실행 중입니다.")

        elif data.get("command") == "end":
            print('[WebSocket] 서버로부터 end 명령 수신 → 감지 중지')
            stop_sleep_detection.set()
            stop_motor_monitoring.set()

            if sleep_detection_thread and sleep_detection_thread.is_alive():
                sleep_detection_thread.join(timeout=5)
                if sleep_detection_thread.is_alive():
                    print("[경고] 졸음 감지 쓰레드가 종료되지 않았습니다.")
                else:
                    sleep_detection_thread = None

            if motor_thread and motor_thread.is_alive():
                motor_thread.join(timeout=5)
                if motor_thread.is_alive():
                    print("[경고] CO2 모니터링 쓰레드가 종료되지 않았습니다.")
                else:
                    motor_thread = None

        elif data.get("command") == "set-sound":
            value = data.get("value").lower()
            global enable_buzzer
            global brightness
            enable_buzzer = (value == "on")
            print(f"[WebSocket] 서버로부터 사운드 설정 수신 → 값: {enable_buzzer}")

        elif data.get("command") == "set-led":
            try:
                brightness = int(data.get("value"))
                if 0 <= brightness <= 100:
                    pwm.ChangeDutyCycle(brightness)
                    print(f"[WebSocket] LED 밝기 설정됨 → {brightness}%")
                else:
                    print(f"[오류] 밝기 값 {brightness}는 0~100 사이여야 합니다.")
            except (ValueError, TypeError):
                print("[오류] 잘못된 밝기 값이 수신되었습니다.")

    except Exception as e:
        print(f"WebSocket 메시지 처리 오류: {e}")


def on_open(ws):
    global ws_world
    ws_world = ws
    serial_number = get_serial_number()
    print(f"[WebSocket] 연결됨 → Serial: {serial_number}")
    ws.send(json.dumps({"type": "raspberry", "serial": serial_number}))


def run_websocket():
    websocket.enableTrace(False)
    while True:
        try:
            print("[WebSocket] 서버 연결 대기 중...")
            ws = websocket.WebSocketApp(
                "ws://15.164.231.109:4141",
                on_open=on_open,
                on_message=on_message
            )
            ws.run_forever()
        except Exception as e:
            print(f"[WebSocket] 재연결 시도 중 오류: {e}")
            global ws_world
            ws_world = None
            time.sleep(5)

websocket_thread = threading.Thread(target=run_websocket)
websocket_thread.start()
websocket_thread.join()