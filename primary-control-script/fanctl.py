#!/usr/bin/env python3

###
# Fan control script for FreeNAS systems with SuperMicro X10 motherboards
# Written by jro
# Based on script by Stux
###

### TODO:
# eventlet on fanctl_disp
# split CPU and HDD code into threads in fanctl
# split rpm, temp, comms code into threads in fanctl_client
# zpool status info on fanctl_disp
# SMART data on fanctl_disp
# fanctl_client device stats (cpu, temp) on fanctl_disp
# fanctl_disp device stats (cpu, temp)
# graphing on fanctl_disp

### Libraries:
# time to get current seconds
# datetime to timestamp log file messages
# subprocess for executing shell commands
# re for regex processing
# sys for logging file access
# signal to close log file on script termination (SIGTERM)
# socket to connect to controllers and display
# psutil to get cpu load info
import time, datetime, subprocess, re, sys, signal, socket, psutil, configparser

### Parse configuration

config = configparser.ConfigParser()
config.read('fanctl.ini')

# CPU and HDD temps (in C) map to duty cycles
cpu_temp_list = [int(x) for x in config['TempsMap']['cpu_temp_list'].split()]
cpu_duty_list = [int(x) for x in config['TempsMap']['cpu_duty_list'].split()]
hd_temp_list = [int(x) for x in config['TempsMap']['hd_temp_list'].split()]
hd_duty_list = [int(x) for x in config['TempsMap']['hd_duty_list'].split()]

# Connections informations
shelvesAccess = [config['ShelvesAccess'][k] for k in config['ShelvesAccess']]

# Misc. variables
try:
	log_file = config['Misc']['log_file']
except:
	log_file = ""
cpu_override_temp = int(config['Misc']['cpu_override_temp'])
cpu_max_fan_speed = int(config['Misc']['cpu_max_fan_speed'])
cpu_fan_header = config['Misc']['cpu_fan_header']
num_chassis = int(config['Misc']['num_chassis'])
num_disks = int(config['Misc']['num_disks'])
hd_polling_interval = int(config['Misc']['hd_polling_interval'])
bmc_fail_threshold = int(config['Misc']['bmc_fail_threshold'])
bmc_reboot_grace_time = int(config['Misc']['bmc_reboot_grace_time'])
debug = config['Misc'].getboolean('debug')
cpu_debug = config['Misc'].getboolean('cpu_debug')

# disk identification info
# Used to map device nodes to physical disk location for arduino display. Starts with disk
# in top left slot of chassis continuing along top row like so:
#
#	[ 01 | 02 | 03 | 04 ]
#	[ 05 | 06 | 07 | 08 ]
#	[    ... etc ...    ]

hd_list = []
shelves = [[]] * num_chassis
for k in config['Disks']:
	diskInfo = config['Disks'][k].split(",")
	disk = {"serial": diskInfo[0], "shelf": int(diskInfo[1]), "position": int(diskInfo[2]), "node": "", "temp": 0}
	hd_list.append(disk)
	shelves[int(diskInfo[1])].append(disk)

### System variables

# OS detection
os = subprocess.check_output("uname").decode("utf-8").replace("\n", "")
if os != "Linux" and os != "FreeBSD":
	sys.exit("Fatal: Operating system " + os + " not supported")

# Initialize other system variables
cpu_fan_unreadable_time = 0
bmc_fail_count = 0
cpu_fan_duty = 0
last_hd_check_time = 0
override_time = 0

# Redirect stdout and stderr to log file
if log_file:
	log = open(log_file,'w')
	sys.stdout = log
	sys.stderr = log

# Generate per-shelf variables
hd_fan_duty = [0] * num_chassis
max_hd_temp = [0] * num_chassis
shelf_tty = [0] * num_chassis

### Pre-loop setup/info gathering

# Attempt to connect to a web socket at IP and port for a given number of attempts; attempts = 0 means indefinite retries
def connectToSocket(sock,ip,port,attempts):
	connected = False
	x = 0
	while connected == False:
		if attempts != 0: x += 1
		try:
			sock.connect((ip, port))
			print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
			print("Connected to " + sock.getpeername()[0],flush=True)
			connected = True
		except:
			print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
			if x < attempts:
				print("ERROR: Could not connect to " + ip + " (attempt #" + str(x) + "). Trying again in 5 seconds.",flush=True)
			else:
				print("ERROR: Could not connect to " + ip + " (attempt #" + str(x) + "). Bailing.",flush=True)
				return
			time.sleep(5)
			pass

# Set BMC fan mode to full allowing for manual control
def set_fan_mode_full():
	subprocess.check_output("ipmitool raw 0x30 0x45 0x01 1",shell=True)
	time.sleep(5)

# BMC reset function called in case of CPU fan errors
def reset_bmc():
	subprocess.check_output("ipmitool bmc reset cold",shell=True)
	time.sleep(5)

# Close log file on SIGTERM
def close_log(signum, frame):
	sys.stdout.close()
	sys.stderr.close()
	for shelf in range(num_chassis):
		shelf_sock[shelf].close()
	disp_sock.close()
	print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - Script terminating.",flush=True)
	sys.exit()

signal.signal(signal.SIGTERM,close_log)

# Print script start time to log file
print("Starting fan control script at " + datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S'),flush=True)

# Set IPMI fan mode to full
print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
print("Setting CPU fan mode to full.",flush=True)
set_fan_mode_full()

# Connect via sockets to the controllers in each shelf
shelf_sock = [""] * num_chassis
port = 10000
for shelf in range(0,num_chassis):
	shelf_sock[shelf] = socket.socket()
	connectToSocket(shelf_sock[shelf],shelvesAccess[shelf],port,0)

# Connect via sockets to the display device
#disp_sock = socket.socket()
#connectToSocket(disp_sock,"10.0.10.100",port,0)

# Populate HD List, get all disks from sysctl -n kern.disks command and split output into list
if os == "Linux":
	node_list = subprocess.check_output("lsblk -ndoNAME",shell=True).decode("utf-8").split("\n")
elif os == "FreeBSD":
	node_list = subprocess.check_output("sysctl -n kern.disks",shell=True).decode("utf-8").replace("\n","").split(" ")
node_list_copy = node_list.copy()

for node in node_list_copy:
	# Attempt to run smartctl -i on each disk, remove the disks that don't support smartctl
	try:
		smart = subprocess.check_output("smartctl -i /dev/" + node,shell=True).decode("utf-8")
	except:
		node_list.remove(node)
		continue

	# Remove SSDs from the list
	try:
		node_type = re.search(r'(.*?Rotation.*?)\n',smart)[0]
	except:
		# Some drives don't report rotation at all, ignore them
		node_list.remove(node)
		continue
	if "Solid State Device" in node_type:
		node_list.remove(node)
	else:
		# Get the serial number via regex search from the smartctl output
		node_serial = re.search(r'(.*?Serial.*?)\n',smart)[0]
		# Search for that serial in the hd_list
		for hd in hd_list:
			if hd["serial"] in node_serial:
				# When it finds a match, put the device node into the dictionary
				hd["node"] = node
				print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
				print("Found disk /dev/" + hd["node"] + " with serial " + hd["serial"] + " in shelf " + str(hd["shelf"]) + " position " + str(hd["position"]),flush=True)
				break

### Main loop
# 1) Check CPU temps with sysctl, map max temp to duty cycle, set duty cycle with ipmitool. If fan speed goes unchanged
# 	for a certain amount of time, verify that the fan speed reading is sane. If not, reset BMC.
# 2) Check HD temps every so often via smartctl, record all temps, determine max temp in each shelf, map max temp to
# 	duty cycle, set duty cycle via socket connection to controller in each shelf and send temp data to display unit.

while True:
	### CPU temp check and duty cycle management
	# Check all CPU core temps, determine max temp, map temp to duty cycle
	try:
		if os == "Linux":
			core_temps = subprocess.check_output("sensors | grep -oP 'Core.*?\+\K[0-9.]+'",shell=True).decode("utf-8").split()
		elif os == "FreeBSD":
			core_temps = subprocess.check_output("sysctl -a dev.cpu | egrep -E \"dev.cpu.[0-9]+.temperature\" | awk \'{print $2}\' | sed \'s/.$//\'",shell=True).decode("utf-8").split()
	except:
		core_temps = []

	# Prefix cpu temp data with "cpu;" so display knows which data type it is
#	cpu_temp_list_str = "cpu;"
#	for temp in core_temps:
#		cpu_temp_list_str += str(int(float(temp))) + " "
#	cpu_temp_list_str = cpu_temp_list_str.strip()

	# Attempt to send CPU temp data to display; if unsuccessful, attempt to reconnect.
#	try:
#		disp_sock.send(cpu_temp_list_str.encode("utf-8"))
#	except:
#		print("ERROR: Could not send to display web socket! Attempting to reconnect now...",flush=True)
#		disp_sock.close()
#		disp_sock = socket.socket()
#		connectToSocket(disp_sock,"10.0.10.100",port,5)

	# Determine max core temp; look up this temp in duty cycle mapping
	if core_temps:
		cpu_temp = int(float(max(core_temps)))
		for temp in cpu_temp_list:
			if cpu_temp <= temp:
				last_cpu_fan_duty = cpu_fan_duty
				cpu_fan_duty = cpu_duty_list[cpu_temp_list.index(temp)]
				break
	else:
		cpu_temp = 0
		cpu_fan_duty = 0
		last_cpu_fan_duty = 0

	# If CPU temp is too high, set HD fans to 100% (enter fan check by resetting HD check time)
	if cpu_temp >= cpu_override_temp:
		hd_fan_override = True
		if int(time.time()) - override_time > hd_polling_interval:
			print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
			print("CPU above HDD fan override threshold of " + str(cpu_override_temp) + "*C, overriding head unit HDD fans to 100%",flush=True)
			override_time = int(time.time())
			last_hd_check_time = 0
	else:
		hd_fan_override = False

	# Set CPU fan duty cycle through IPMI if new duty cycle selected
	if cpu_fan_duty != last_cpu_fan_duty:
		if cpu_debug:
			print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
			print("CPU at " + str(cpu_temp) + "*C, setting CPU fans " + str(cpu_fan_duty) + "%",flush=True)
		subprocess.check_output("ipmitool raw 0x30 0x70 0x66 0x01 0 " + str(cpu_fan_duty),shell=True)
		last_cpu_fan_change_time = int(time.time())

	### CPU fan speed verification
	# Check that fan speed is being reported by ipmitool and that the reading is non-zero and not above max fan speed
	cpu_fan_speed = subprocess.check_output("ipmitool sdr | grep " + cpu_fan_header,shell=True).decode("utf-8").split()[2]

	# Use this data to send fan speed data to display. Attempt to send data to display; attempt to reconnect on failure
#	cpu_fan_disp = "cpu_fans;Fans " + str(cpu_fan_duty) + "% @ " + str(cpu_fan_speed) + " RPM;" + str(psutil.cpu_percent())
#	try:
#		disp_sock.send(cpu_fan_disp.encode("utf-8"))
#	except:
#		print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
#		print("ERROR: Could not send to display web socket! Attempting to reconnect now...",flush=True)
#		disp_sock.close()
#		disp_sock = socket.socket()
#		connectToSocket(disp_sock,"10.0.10.100",port,5)

	# Detect errors with output of cpu fan check command
	if cpu_fan_speed == "no" or cpu_fan_speed == "disabled":
		cpu_fan_speed = -1
	try: cpu_fan_speed = int(cpu_fan_speed)
	except: cpu_fan_speed = -1

	# If fan reading reported an error/no reading, fan speed will be -1. Could be because of BMC reset, so give it some time
	if cpu_fan_speed < 0:
		if cpu_fan_unreadable_time == 0:
			cpu_fan_unreadable_time = int(time.time())
			print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
			print("ERROR: Fan currently unreadable, waiting for BMC reboot grace period",flush=True)
		if int(time.time()) - cpu_fan_unreadable_time > bmc_reboot_grace_time:
			print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
			print("ERROR: Fan currently unreadable, BMC reboot grace period elapsed, cold resetting BMC",flush=True)
			set_fan_mode_full()
			reset_bmc()
			cpu_fan_unreadable_time = 0

	# If we did get a fan speed reading, check that it is sane (i.e., not 0 and not above cpu_max_fan_speed by >20% margin)
	else:
		cpu_fan_unreadable_time = 0

		# If fan reading is not sane, reset BMC after enough consecutive nonsense readings
		if cpu_fan_speed == 0 or cpu_fan_speed > cpu_max_fan_speed * 1.2:
			bmc_fail_count += 1
		else:
			bmc_fail_count = 0

		# If we get a single bad reading, just try to reset BMC fan mode and CPU fan duty cycle
		if bmc_fail_count > 0 and bmc_fail_count <= bmc_fail_threshold:
			print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
			print("ERROR: CPU fan reading is " + str(cpu_fan_speed) + " RPM. BMC fail count at " + str(bmc_fail_count) + "/" + str(bmc_fail_threshold) + ".", end = "")
			print(" Attempting to set fan mode and apply " + str(cpu_fan_duty) + "% duty cycle again.",flush=True)
			set_fan_mode_full()
			subprocess.check_output("ipmitool raw 0x30 0x70 0x66 0x01 0 " + str(cpu_fan_duty),shell=True)
		# If we get enough bad readings, reset BMC fan mode and cold reset BMC
		elif bmc_fail_count > bmc_fail_threshold:
			print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
			print("ERROR: CPU fan reading is " + str(cpu_fan_speed) + " RPM. BMC fail count at " + str(bmc_fail_count) + "/" + str(bmc_fail_threshold) + ". Cold resetting BMC.",flush=True)
			set_fan_mode_full()
			reset_bmc()
			bmc_fail_count = 0

	### HD temp check and duty cycle management
	# Only check drive temps every so often
	if int(time.time()) - last_hd_check_time > hd_polling_interval:
		last_hd_check_time = int(time.time())

		# Zero HD max temps for each shelf
		for x in range(0,len(max_hd_temp)): max_hd_temp[x] = 0

		# Get all disk temps by running smartctl on each disk
		hd_temps = "hdd;"
		for hd in hd_list:
			try:
				disk_temp = subprocess.check_output("smartctl -A /dev/" + hd["node"] + " | grep Temperature_Celsius",shell=True)
				disk_temp = disk_temp.decode("utf-8").replace("\n","")
				hd["temp"] = int(disk_temp.split()[9])
				hd_temps += str(hd["temp"]) + " "
			except:
				hd["temp"] = 0
				pass
			for shelf in range(0,num_chassis):
				if hd["shelf"] == shelf and hd["temp"] > max_hd_temp[shelf]:
					max_hd_temp[shelf] = hd["temp"]
		hd_temps = hd_temps.strip()

		# Correct for empty HDD bays
		for hd in hd_list:
			if hd["temp"] == 0:
				hd["temp"] = "--"

		# Map disk temps to duty cycle for each shelf
		for shelf in range(0,num_chassis):
			for temp in hd_temp_list:
				if max_hd_temp[shelf] <= temp:
					hd_fan_duty[shelf] = hd_duty_list[hd_temp_list.index(temp)]
					break

		# If hd_fan_override triggered, set fan duty cycle for shelf 0 (head) to 100
		if hd_fan_override: hd_fan_duty[0] = 100

		# Print drive temps and fan duty cycles to log
		if debug:
			for shelf in range(0,num_chassis):
				print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
				print("Shelf " + str(shelf) + " max temp: " + str(max_hd_temp[shelf]) + "*C, setting HDD fans to " + str(hd_fan_duty[shelf]) + "%",flush=True)

		# Send HDD fan speed values to display. Attempt to reconnect on error.
#		if debug:
#			print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
#			print("Sending to display: " + hd_temps,flush=True)
#		try:
#			disp_sock.send(hd_temps.encode("utf-8"))
#		except:
#			print("ERROR: Could not send to display web socket! Attempting to reconnect now...",flush=True)
#			disp_sock.close()
#			disp_sock = socket.socket()
#			connectToSocket(disp_sock,"10.0.10.100",port,5)

		# Send HDD fan speed commands to controllers. Attempt to reconnect on error.
		for x in range(0,num_chassis):
			if debug:
				print(datetime.datetime.today().strftime('%m-%d-%Y %H:%M:%S') + " - ", end = "")
				print("Sending to shelf " + str(x) +": " + str(hd_fan_duty[x]),flush=True)
			try:
				shelf_sock[x].send(str(hd_fan_duty[x]).encode("utf-8"))
			except:
				print("ERROR: Could not send to shelf " + str(x) + " web socket! Attempting to reconnect now...",flush=True)
				shelf_sock[x].close()
				shelf_sock[x] = socket.socket()
				connectToSocket(shelf_sock[x],shelvesAccess[x],port,5)

	# Provide a small grace period between iterations; temps don't change much in a one-second span
	time.sleep(1)

