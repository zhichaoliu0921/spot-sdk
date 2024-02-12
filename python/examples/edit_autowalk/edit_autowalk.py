# Copyright (c) 2023 Boston Dynamics, Inc.  All rights reserved.
#
# Downloading, reproducing, distributing or otherwise using the SDK Software
# is subject to the terms and conditions of the Boston Dynamics Software
# Development Kit License (20191101-BDSDK-SL).

"""
Autowalk Replay Script.  Command-line utility to edit and replay stored Autowalk missions.
"""

import argparse
import os
import sys
import time

import PyQt5.QtCore as QtCore
import PyQt5.QtWidgets as QtWidgets

import bosdyn.api.mission
import bosdyn.client
import bosdyn.client.lease
import bosdyn.client.util
import bosdyn.geometry
import bosdyn.mission.client
import bosdyn.util
from bosdyn.api.autowalk import autowalk_pb2, walks_pb2
from bosdyn.api.graph_nav import graph_nav_pb2, map_pb2, nav_pb2
from bosdyn.api.mission import mission_pb2
from bosdyn.client.power import PowerClient, power_on_motors
from bosdyn.client.robot_command import RobotCommandClient, blocking_stand
from bosdyn.client.robot_state import RobotStateClient


def main():
    """Edit and replay stored autowalks with command-line interface"""

    body_lease = None

    # Configure logging
    bosdyn.client.util.setup_logging()

    # Parse command-line arguments
    parser = argparse.ArgumentParser()

    bosdyn.client.util.add_base_arguments(parser)

    parser.add_argument('--fail_on_question', action='store_true', default=False,
                        dest='fail_on_question', help='Enables failing mission if question')
    parser.add_argument(
        '--timeout', type=float, default=3.0, dest='timeout',
        help='''Mission client timeout (s). Robot will pause mission execution if
                         a new play request is not received within this time frame''')
    parser.add_argument('--noloc', action='store_true', default=False, dest='noloc',
                        help='Skip initial localization')
    parser.add_argument('--static', action='store_true', default=False, dest='static_mode',
                        help='Stand, but do not run robot')
    parser.add_argument('--walk_directory', dest='walk_directory', required=True,
                        help='Directory ending in .walk containing Autowalk files')
    parser.add_argument(
        '--walk_filename', dest='walk_filename', required=True, help=
        'Autowalk mission filename. Script assumes the path to this file is [walk_directory]/missions/[walk_filename]'
    )

    args = parser.parse_args()

    path_following_mode = map_pb2.Edge.Annotations.PATH_MODE_UNKNOWN
    fail_on_question = args.fail_on_question
    do_localization = False if args.noloc else True
    walk_directory = args.walk_directory
    autowalk_file = f'{walk_directory}/missions/{args.walk_filename}'
    print(
        f'[ REPLAYING AUTOWALK MISSION {autowalk_file} : WALK DIRECTORY {walk_directory} : HOSTNAME {args.hostname} ]'
    )

    # Initialize robot object
    robot = init_robot(args.hostname)

    if not os.path.isfile(autowalk_file):
        robot.logger.fatal('Unable to find autowalk file: %s.', autowalk_file)
        sys.exit(1)

    if not os.path.isdir(walk_directory):
        robot.logger.fatal('Unable to find walk directory: %s.', walk_directory)
        sys.exit(1)

    # Open GUI to edit autowalk
    walk = create_and_edit_autowalk(autowalk_file, robot.logger)

    assert not robot.is_estopped(), 'Robot is estopped. Please use an external E-Stop client, such as the estop SDK ' \
                                    'example, to configure E-Stop.'

    # Initialize clients
    power_client, robot_state_client, command_client, mission_client, graph_nav_client, autowalk_client = init_clients(
        robot, walk_directory)

    # Acquire robot lease
    robot.logger.info('Acquiring lease...')
    lease_client = robot.ensure_client(bosdyn.client.lease.LeaseClient.default_service_name)

    with bosdyn.client.lease.LeaseKeepAlive(lease_client, must_acquire=True, return_at_exit=True):
        # Upload autowalk mission to robot
        upload_autowalk(robot.logger, autowalk_client, walk)

        # Turn on power
        power_on_motors(power_client)

        # Stand up and wait for the perception system to stabilize
        robot.logger.info('Commanding robot to stand...')
        blocking_stand(command_client, timeout_sec=20)
        countdown(5)
        robot.logger.info('Robot standing.')

        # Localize robot
        if do_localization:
            graph = graph_nav_client.download_graph()
            robot.logger.info('Localizing robot...')
            robot_state = robot_state_client.get_robot_state()
            localization = nav_pb2.Localization()

            # Attempt to localize using any visible fiducial
            graph_nav_client.set_localization(
                initial_guess_localization=localization, ko_tform_body=None, max_distance=None,
                max_yaw=None,
                fiducial_init=graph_nav_pb2.SetLocalizationRequest.FIDUCIAL_INIT_NEAREST)

        # Run autowalk
        if not args.static_mode:
            run_autowalk(robot.logger, mission_client, fail_on_question, args.timeout,
                         path_following_mode)


def init_robot(hostname):
    """Initialize robot object"""

    # Initialize SDK
    sdk = bosdyn.client.create_standard_sdk('AutowalkReplay', [bosdyn.mission.client.MissionClient])

    # Create robot object
    robot = sdk.create_robot(hostname)

    # Authenticate with robot
    bosdyn.client.util.authenticate(robot)

    # Establish time sync with the robot
    robot.time_sync.wait_for_sync()

    return robot


def init_clients(robot, walk_directory):
    """Initialize clients"""

    # Initialize power client
    robot.logger.info('Starting power client...')
    power_client = robot.ensure_client(PowerClient.default_service_name)

    # Create graph-nav client
    robot.logger.info('Creating graph-nav client...')
    graph_nav_client = robot.ensure_client(
        bosdyn.client.graph_nav.GraphNavClient.default_service_name)

    # Clear map state and localization
    robot.logger.info('Clearing graph-nav state...')
    graph_nav_client.clear_graph()

    # Upload map to robot
    upload_graph_and_snapshots(robot.logger, graph_nav_client, walk_directory)

    # Create mission client
    robot.logger.info('Creating mission client...')
    mission_client = robot.ensure_client(bosdyn.mission.client.MissionClient.default_service_name)

    # Create autowalk client
    autowalk_client = robot.ensure_client(
        bosdyn.client.autowalk.AutowalkClient.default_service_name)

    # Create command client
    robot.logger.info('Creating command client...')
    command_client = robot.ensure_client(RobotCommandClient.default_service_name)

    # Create robot state client
    robot.logger.info('Creating robot state client...')
    robot_state_client = robot.ensure_client(RobotStateClient.default_service_name)

    return power_client, robot_state_client, command_client, mission_client, graph_nav_client, autowalk_client


def countdown(length):
    """Print sleep countdown"""

    for i in range(length, 0, -1):
        print(i, end=' ', flush=True)
        time.sleep(1)
    print(0)


def create_and_edit_autowalk(filename, logger):
    """Creates autowalk from file and opens GUI for editing"""

    walk = walks_pb2.Walk()

    with open(filename, 'rb') as autowalk_file:
        data = autowalk_file.read()
        walk.ParseFromString(data)

    app = QtWidgets.QApplication(sys.argv)
    gui = AutowalkGUI(walk)
    gui.setWindowFlags(QtCore.Qt.WindowStaysOnTopHint)
    gui.show()
    gui.resize(540, 320)
    app.exec_()

    if not walk.elements:
        logger.fatal('Autowalk cancelled due to empty walk or user input')
        sys.exit(1)

    return walk


def upload_graph_and_snapshots(logger, client, path):
    """Upload the graph and snapshots to the robot"""

    # Load the graph from disk.
    graph_filename = os.path.join(path, 'graph')
    logger.info('Loading graph from %s', graph_filename)

    with open(graph_filename, 'rb') as graph_file:
        data = graph_file.read()
        current_graph = map_pb2.Graph()
        current_graph.ParseFromString(data)
        logger.info('Loaded graph has %d waypoints and %d edges', len(current_graph.waypoints),
                    len(current_graph.edges))

    # Load the waypoint snapshots from disk.
    current_waypoint_snapshots = dict()
    for waypoint in current_graph.waypoints:
        if len(waypoint.snapshot_id) == 0:
            continue
        snapshot_filename = os.path.join(path, 'waypoint_snapshots', waypoint.snapshot_id)
        logger.info('Loading waypoint snapshot from %s', snapshot_filename)

        with open(snapshot_filename, 'rb') as snapshot_file:
            waypoint_snapshot = map_pb2.WaypointSnapshot()
            waypoint_snapshot.ParseFromString(snapshot_file.read())
            current_waypoint_snapshots[waypoint_snapshot.id] = waypoint_snapshot

    # Load the edge snapshots from disk.
    current_edge_snapshots = dict()
    for edge in current_graph.edges:
        if len(edge.snapshot_id) == 0:
            continue
        snapshot_filename = os.path.join(path, 'edge_snapshots', edge.snapshot_id)
        logger.info('Loading edge snapshot from %s', snapshot_filename)

        with open(snapshot_filename, 'rb') as snapshot_file:
            edge_snapshot = map_pb2.EdgeSnapshot()
            edge_snapshot.ParseFromString(snapshot_file.read())
            current_edge_snapshots[edge_snapshot.id] = edge_snapshot

    # Upload the graph to the robot.
    logger.info('Uploading the graph and snapshots to the robot...')
    no_anchors = not len(current_graph.anchoring.anchors)
    response = client.upload_graph(graph=current_graph, generate_new_anchoring=no_anchors)
    logger.info('Uploaded graph.')

    # Upload the snapshots to the robot.
    for snapshot_id in response.unknown_waypoint_snapshot_ids:
        waypoint_snapshot = current_waypoint_snapshots[snapshot_id]
        client.upload_waypoint_snapshot(waypoint_snapshot=waypoint_snapshot)
        logger.info('Uploaded %s', waypoint_snapshot.id)

    for snapshot_id in response.unknown_edge_snapshot_ids:
        edge_snapshot = current_edge_snapshots[snapshot_id]
        client.upload_edge_snapshot(edge_snapshot=edge_snapshot)
        logger.info('Uploaded %s', edge_snapshot.id)


def upload_autowalk(logger, autowalk_client, walk):
    """Upload the autowalk mission to the robot"""

    logger.info('Uploading the autowalk to the robot...')
    autowalk_result = autowalk_client.load_autowalk(walk)

    logger.info('Autowalk upload succeeded')
    return autowalk_result.status == autowalk_pb2.LoadAutowalkResponse.STATUS_OK


def run_autowalk(logger, mission_client, fail_on_question, mission_timeout, path_following_mode):
    """Run autowalk"""

    logger.info('Running autowalk')

    mission_state = mission_client.get_state()

    while mission_state.status in (mission_pb2.State.STATUS_NONE, mission_pb2.State.STATUS_RUNNING):
        # We optionally fail if any questions are triggered.
        # This often indicates a problem in Autowalk missions
        if mission_state.questions and fail_on_question:
            logger.info('Mission failed by triggering operator question: %s',
                        mission_state.questions)
            return False

        local_pause_time = time.time() + mission_timeout

        play_settings = mission_pb2.PlaySettings(path_following_mode=path_following_mode)

        mission_client.play_mission(local_pause_time, settings=play_settings)
        time.sleep(1)

        mission_state = mission_client.get_state()

    logger.info('Mission status = %s', mission_state.Status.Name(mission_state.status))

    return mission_state.status in (mission_pb2.State.STATUS_SUCCESS,
                                    mission_pb2.State.STATUS_PAUSED)


class ListWidget(QtWidgets.QListWidget):
    """List object for GUI"""

    def __init__(self, type, parent=None, isMutable=False):
        super(ListWidget, self).__init__(parent)
        self.setIconSize(QtCore.QSize(124, 124))
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDrop)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setAcceptDrops(isMutable)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            super(ListWidget, self).dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(QtCore.Qt.CopyAction)
            event.accept()
        else:
            super(ListWidget, self).dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(QtCore.Qt.CopyAction)
            event.accept()
            links = []
            for url in event.mimeData().urls():
                links.append(str(url.toLocalFile()))
            self.emit(QtCore.SIGNAL('dropped'), links)
        else:
            event.setDropAction(QtCore.Qt.MoveAction)
            super(ListWidget, self).dropEvent(event)


class AutowalkGUI(QtWidgets.QMainWindow):
    """GUI for editing autowalk"""

    def __init__(self, walk):
        super(QtWidgets.QMainWindow, self).__init__()
        self.walk = walk
        self.walk_name_to_element = {element.name: element for element in walk.elements}

        # Create and format GUI window
        myQWidget = QtWidgets.QWidget()
        myOuterBoxLayout = QtWidgets.QVBoxLayout()
        myQWidget.setLayout(myOuterBoxLayout)
        self.setCentralWidget(myQWidget)

        mediumWidget = QtWidgets.QWidget()
        myMediumBoxLayout = QtWidgets.QHBoxLayout()
        mediumWidget.setLayout(myMediumBoxLayout)

        # List widget with available autowalk actions
        self.sourceLabelWidget = QtWidgets.QLabel(self)
        self.sourceLabelWidget.setText('Available Actions')
        self.sourceLabelWidget.setAlignment(QtCore.Qt.AlignCenter)
        self.sourceListWidget = ListWidget(self)

        self.copyAllButton = QtWidgets.QPushButton('Copy All', self)

        # Populates with current actions from autowalk file
        for element in walk.elements:
            QtWidgets.QListWidgetItem(element.name, self.sourceListWidget)

        sourceWidget = QtWidgets.QWidget()
        sourceBoxLayout = QtWidgets.QVBoxLayout()
        sourceWidget.setLayout(sourceBoxLayout)
        sourceBoxLayout.addWidget(self.sourceLabelWidget)
        sourceBoxLayout.addWidget(self.sourceListWidget)
        sourceBoxLayout.addWidget(self.copyAllButton)

        myMediumBoxLayout.addWidget(sourceWidget)

        # List widget with modified autowalk
        self.actionLabelWidget = QtWidgets.QLabel(self)
        self.actionLabelWidget.setText('Current Autowalk')
        self.actionLabelWidget.setAlignment(QtCore.Qt.AlignCenter)
        self.actionListWidget = ListWidget(self, isMutable=True)

        actionWidget = QtWidgets.QWidget()
        actionBoxLayout = QtWidgets.QVBoxLayout()
        actionWidget.setLayout(actionBoxLayout)
        self.deleteButton = QtWidgets.QPushButton('Delete Selected', self)
        actionBoxLayout.addWidget(self.actionLabelWidget)
        actionBoxLayout.addWidget(self.actionListWidget)
        actionBoxLayout.addWidget(self.deleteButton)
        myMediumBoxLayout.addWidget(actionWidget)

        # Layout for settings
        self.settingsWidget = QtWidgets.QWidget()
        self.settingsBoxLayout = QtWidgets.QVBoxLayout()
        self.settingsWidget.setLayout(self.settingsBoxLayout)
        self.settingsBoxLayout.setAlignment(QtCore.Qt.AlignTop)

        self.settingsLabelWidget = QtWidgets.QLabel(self)
        self.settingsLabelWidget.setText('Play Autowalk')
        self.settingsLabelWidget.setAlignment(QtCore.Qt.AlignCenter)
        self.repeatsComboBox = QtWidgets.QComboBox(self)
        self.repeatsComboBox.addItems(['Once', 'Periodically', 'Continuously'])
        self.skipDockingBox = QtWidgets.QCheckBox('Skip docking')
        self.intervalLabel = None
        self.intervalLine = None
        self.repeatLabel = None
        self.repeatLine = None

        self.settingsBoxLayout.addWidget(self.settingsLabelWidget)
        self.settingsBoxLayout.addWidget(self.repeatsComboBox)
        self.settingsBoxLayout.addWidget(self.skipDockingBox)

        myMediumBoxLayout.addWidget(self.settingsWidget)

        myOuterBoxLayout.addWidget(mediumWidget)

        # Widget for buttons
        self.applyButton = QtWidgets.QPushButton('Apply', self)
        self.cancelButton = QtWidgets.QPushButton('Cancel', self)
        buttonWidget = QtWidgets.QWidget()
        buttonBoxLayout = QtWidgets.QHBoxLayout()
        buttonWidget.setLayout(buttonBoxLayout)
        buttonBoxLayout.addWidget(self.cancelButton)
        buttonBoxLayout.addWidget(self.applyButton)
        myOuterBoxLayout.addWidget(buttonWidget)

        # Signal handling
        self.repeatsComboBox.activated.connect(self.change_play_window)
        self.copyAllButton.clicked.connect(self.copy_all)
        self.deleteButton.clicked.connect(self.delete_action)
        self.applyButton.clicked.connect(self.apply_changes)
        self.cancelButton.clicked.connect(self.cancel_application)

        self.setWindowTitle('Drag and Drop Autowalk')

    def cancel_application(self):
        """Clears walk by modifying protocol buffer object"""
        del self.walk.elements[:]
        self.close()

    def copy_all(self):
        """Copies all actions to current autowalk list"""
        self.actionListWidget.clear()
        for element in self.walk.elements:
            QtWidgets.QListWidgetItem(element.name, self.actionListWidget)

    def delete_action(self):
        """Removes selected action from current autowalk list"""
        selectedItems = self.actionListWidget.selectedItems()
        for item in selectedItems:
            row = self.actionListWidget.row(item)
            self.actionListWidget.takeItem(row)

    def apply_changes(self):
        """Modifies walk according to final autowalk list"""

        # Clear current walk's elements
        del self.walk.elements[:]

        # Maps list of action names to element protocol objects
        element_names = []
        for row in range(self.actionListWidget.count()):
            element_names.append(self.actionListWidget.item(row).text())
        elements = [self.walk_name_to_element[name] for name in element_names]

        # Sets new elements of walk
        self.walk.elements.extend(elements)

        # If "once" is selected, check if docking should be skipped
        if self.repeatsComboBox.currentIndex() == 0:
            self.walk.playback_mode.once.skip_docking_after_completion = self.skipDockingBox.isChecked(
            )
        # If "periodically" is selected, set the interval and repetitions that were input
        elif self.repeatsComboBox.currentIndex() == 1:
            intervalInput = int(self.intervalLine.text().strip())
            repeatInput = int(self.repeatLine.text().strip())
            self.walk.playback_mode.periodic.interval.seconds = intervalInput
            self.walk.playback_mode.periodic.repetitions = repeatInput
        # If "continuous" is selected
        else:
            self.walk.playback_mode.continuous.SetInParent()

        self.close()

    def change_play_window(self, index):
        """Changes the panel for repetitions of autowalk"""
        # 'Once' is selected
        if index == 0:
            # Clears interval GUI options
            if self.intervalLabel:
                self.settingsBoxLayout.removeWidget(self.intervalLabel)
                self.intervalLabel.deleteLater()
                self.intervalLabel = None
                self.settingsBoxLayout.removeWidget(self.intervalLine)
                self.intervalLine.deleteLater()
                self.intervalLine = None
                self.settingsBoxLayout.removeWidget(self.repeatLabel)
                self.repeatLabel.deleteLater()
                self.repeatLabel = None
                self.settingsBoxLayout.removeWidget(self.repeatLine)
                self.repeatLine.deleteLater()
                self.repeatLine = None

            # Adds docking box to GUI
            if not self.skipDockingBox:
                self.skipDockingBox = QtWidgets.QCheckBox('Skip docking')
                self.settingsBoxLayout.addWidget(self.skipDockingBox)

        # 'Periodically' is selected
        elif index == 1:
            # Removes docking box
            if self.skipDockingBox:
                self.settingsBoxLayout.removeWidget(self.skipDockingBox)
                self.skipDockingBox.deleteLater()
                self.skipDockingBox = None

            # Adds interval and repetition input boxes if needed
            if not self.intervalLabel:
                self.intervalLabel = QtWidgets.QLabel(self)
                self.intervalLabel.setText('Time Interval (s):')
                self.intervalLine = QtWidgets.QLineEdit()
                self.repeatLabel = QtWidgets.QLabel(self)
                self.repeatLabel.setText('Repetitions:')
                self.repeatLine = QtWidgets.QLineEdit()

                self.settingsBoxLayout.addWidget(self.intervalLabel)
                self.settingsBoxLayout.addWidget(self.intervalLine)
                self.settingsBoxLayout.addWidget(self.repeatLabel)
                self.settingsBoxLayout.addWidget(self.repeatLine)

        # 'Continuously' is selected
        else:
            # Removes all other GUI inputs if necessary
            if self.intervalLabel:
                self.settingsBoxLayout.removeWidget(self.intervalLabel)
                self.intervalLabel.deleteLater()
                self.intervalLabel = None
                self.settingsBoxLayout.removeWidget(self.intervalLine)
                self.intervalLine.deleteLater()
                self.intervalLine = None
                self.settingsBoxLayout.removeWidget(self.repeatLabel)
                self.repeatLabel.deleteLater()
                self.repeatLabel = None
                self.settingsBoxLayout.removeWidget(self.repeatLine)
                self.repeatLine.deleteLater()
                self.repeatLine = None
            if self.skipDockingBox:
                self.settingsBoxLayout.removeWidget(self.skipDockingBox)
                self.skipDockingBox.deleteLater()
                self.skipDockingBox = None


if __name__ == '__main__':
    main()
