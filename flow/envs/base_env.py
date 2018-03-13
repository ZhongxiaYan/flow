import logging
import os
import signal
import subprocess
import sys
from copy import deepcopy
import time
import traceback
import numpy as np
import random

import traci
from traci import constants as tc
import gym

import sumolib

try:
    # Import serializable if rllab is installed
    from rllab.core.serializable import Serializable
except ImportError:
    Serializable = object

try:
    # Load user config if exists, else load default config
    import flow.core.config as config
except ImportError:
    import flow.config_default as config

from flow.core.util import ensure_dir

# Number of retries on restarting SUMO before giving up
RETRIES_ON_ERROR = 10

# Colors are [red, green, yellow, cyan, purple, white]
COLORS = [(255, 0, 0, 255), (0, 255, 0, 255), (255, 255, 0, 255),
          (0, 255, 255, 255), (255, 0, 255, 255), (255, 255, 255, 255)]


class Env(gym.Env, Serializable):
    def __init__(self, env_params, sumo_params, scenario):
        """
        Base environment class. Provides the interface for controlling a SUMO
        simulation. Using this class, you can start sumo, provide a scenario to
        specify a configuration and controllers, perform simulation steps, and
        reset the simulation to an initial configuration.

        SumoEnvironment is Serializable to allow for pickling of the policy.

        This class cannot be used as is: you must extend it to implement an
        action applicator method, and properties to define the MDP if you choose
        to use it with RLLab. This can be done by overloading the following
        functions in a child class:
         - action_space
         - observation_space
         - apply_rl_action
         - get_state
         - compute_reward

        Attributes
        ----------
        env_params: EnvParams type:
           see flow/core/params.py
        sumo_params: SumoParams type
           see flow/core/params.py
        scenario: Scenario type
            see flow/scenarios/base_scenario.py
        """
        # Invoke serializable if using rllab
        if Serializable is not object:
            Serializable.quick_init(self, locals())

        self.env_params = env_params
        self.scenario = scenario
        self.sumo_params = sumo_params
        self.vehicles = scenario.vehicles
        self.traffic_lights = scenario.traffic_lights
        # time_counter: number of steps taken since the start of a rollout
        self.time_counter = 0
        # step_counter: number of total steps taken
        self.step_counter = 0
        # initial_state:
        #   Key = Vehicle ID,
        #   Entry = (type_id, route_id, lane_index, lane_pos, speed, pos)
        self.initial_state = {}
        self.state = None
        self.obs_var_labels = []

        # simulation step size
        self.sim_step = sumo_params.sim_step

        self.vehicle_arrangement_shuffle = \
            env_params.vehicle_arrangement_shuffle
        self.starting_position_shuffle = env_params.starting_position_shuffle

        self.max_speed = env_params.max_speed
        self.lane_change_duration = \
            env_params.get_lane_change_duration(self.sim_step)
        self.shared_reward = env_params.shared_reward
        self.shared_policy = env_params.shared_policy

        # the available_routes variable contains a dictionary of routes vehicles
        # can traverse; to be used when routes need to be chosen dynamically
        self.available_routes = self.scenario.generator.rts

        # TraCI connection used to communicate with sumo
        self.traci_connection = None

        # dictionary of initial observations used while resetting vehicles after
        # each rollout
        self.initial_observations = dict.fromkeys(self.vehicles.get_ids())

        # store the initial vehicle ids
        self.initial_ids = deepcopy(self.vehicles.get_ids())

        # colors used to distinguish between types of vehicles in the network
        self.colors = {}

        # contains the subprocess.Popen instance used to start traci
        self.sumo_proc = None

        self.start_sumo()
        self.setup_initial_state()

    def restart_sumo(self, sumo_params, sumo_binary=None):
        """
        Restarts an already initialized environment. Used when visualizing a
        rollout.
        """
        self.traci_connection.close(False)

        if sumo_binary is not None:
            self.sumo_params.sumo_binary = sumo_binary

        self.sumo_params.port = sumolib.miscutils.getFreeSocketPort()

        # TODO(ak): replace input with emission_path, but make sure this doesn't
        # break visualizer_rllab.py
        if sumo_params.emission_path is not None:
            ensure_dir(sumo_params.emission_path)
            self.sumo_params.emission_path = sumo_params.emission_path

        self.start_sumo()
        self.setup_initial_state()

    def start_sumo(self):
        """
        Starts a sumo instance using the configuration files created by the
        generator class. Also initializes a traci connection to interface with
        sumo from Python.
        """
        # port number the sumo instance will be run on
        if self.sumo_params.port is not None:
            port = self.sumo_params.port
        else:
            port = sumolib.miscutils.getFreeSocketPort()

        # command used to start sumo
        sumo_call = [self.sumo_params.sumo_binary,
                     "-c", self.scenario.cfg,
                     "--remote-port", str(port),
                     "--step-length", str(self.sim_step)]

        # add step logs (if requested)
        if self.sumo_params.no_step_log:
            sumo_call.append("--no-step-log")

        # add the lateral resolution of the sublanes (if requested)
        if self.sumo_params.lateral_resolution is not None:
            sumo_call.append("--lateral-resolution")
            sumo_call.append(str(self.sumo_params.lateral_resolution))

        # add the emission path to the sumo command (if requested)
        if self.sumo_params.emission_path is not None:
            ensure_dir(self.sumo_params.emission_path)
            emission_out = \
                self.sumo_params.emission_path + "{0}-emission.xml".format(
                    self.scenario.name)
            sumo_call.append("--emission-output")
            sumo_call.append(emission_out)
        else:
            emission_out = None

        if self.sumo_params.overtake_right:
            sumo_call.append("--lanechange.overtake-right")
            sumo_call.append("true")

        if self.sumo_params.ballistic:
            sumo_call.append("--step-method.ballistic")
            sumo_call.append("true")

        # specify a simulation seed (if requested)
        if self.sumo_params.seed is not None:
            sumo_call.append("--seed")
            sumo_call.append(str(self.sumo_params.seed))

        logging.info(" Starting SUMO on port " + str(port))
        logging.debug(" Cfg file: " + str(self.scenario.cfg))
        logging.debug(" Emission file: " + str(emission_out))
        logging.debug(" Step length: " + str(self.sim_step))

        error = None
        for _ in range(RETRIES_ON_ERROR):
            try:
                # Opening the I/O thread to SUMO
                self.sumo_proc = subprocess.Popen(sumo_call,
                                                  stdout=sys.stdout,
                                                  stderr=sys.stderr,
                                                  preexec_fn=os.setsid)

                # wait a small period of time for the subprocess to activate
                # before trying to connect with traci
                if os.environ.get("TEST_FLAG", 0):
                    time.sleep(0.1)
                else:
                    time.sleep(config.SUMO_SLEEP)

                self.traci_connection = traci.connect(port, numRetries=100)

                self.traci_connection.simulationStep()
                return
            except Exception as e:
                print("Error during start: {}".format(traceback.format_exc()))
                error = e
                self.teardown_sumo()
        raise error

    def setup_initial_state(self):
        """Returns information on the initial state of the vehicles in the
        network, to be used upon reset.

        Also adds initial state information to the self.vehicles class and
        starts a subscription with sumo to collect state information each step.

        Returns
        -------
        initial_observations: dictionary
            key = vehicles IDs
            value = state describing car at the start of the rollout
        initial_state: dictionary
            key = vehicles IDs
            value = sparse state information (only what is needed to add a
            vehicle in a sumo network with traci)
        """
        # check to make sure all vehicles have been spawned
        num_spawned_veh = self.traci_connection.simulation.getDepartedNumber()
        if num_spawned_veh < self.vehicles.num_vehicles:
            logging.error("Not enough vehicles have spawned! Bad start?")
            exit()

        # add missing traffic lights in the list of traffic light ids
        tls_ids = self.traci_connection.trafficlights.getIDList()

        for tl_id in list(set(tls_ids) - set(self.traffic_lights.get_ids())):
            self.traffic_lights.add(tl_id)

        # subscribe the requested states for traci-related speedups
        for veh_id in self.vehicles.get_ids():
            self.traci_connection.vehicle.subscribe(
                veh_id, [tc.VAR_LANE_INDEX, tc.VAR_LANEPOSITION,
                         tc.VAR_ROAD_ID, tc.VAR_SPEED, tc.VAR_EDGES])
            self.traci_connection.vehicle.subscribeLeader(veh_id, 2000)

        # subscribe some simulation parameters needed to check for entering,
        # exiting, and colliding vehicles
        self.traci_connection.simulation.subscribe(
            [tc.VAR_DEPARTED_VEHICLES_IDS, tc.VAR_ARRIVED_VEHICLES_IDS,
             tc.VAR_TELEPORT_STARTING_VEHICLES_IDS])

        # subscribe the traffic light
        for node_id in self.traffic_lights.get_ids():
            self.traci_connection.trafficlights.subscribe(
                node_id, [tc.TL_RED_YELLOW_GREEN_STATE])

        for veh_id in self.vehicles.get_ids():
            # some constant vehicle parameters to the vehicles class
            self.vehicles.set_state(
                veh_id, "length",
                self.traci_connection.vehicle.getLength(veh_id))
            self.vehicles.set_state(veh_id, "max_speed", self.max_speed)

            # import initial state data to initial_observations dict
            self.initial_observations[veh_id] = dict()
            self.initial_observations[veh_id]["type"] = \
                self.vehicles.get_state(veh_id, "type")
            self.initial_observations[veh_id]["edge"] = \
                self.traci_connection.vehicle.getRoadID(veh_id)
            self.initial_observations[veh_id]["position"] = \
                self.traci_connection.vehicle.getLanePosition(veh_id)
            self.initial_observations[veh_id]["lane"] = \
                self.traci_connection.vehicle.getLaneIndex(veh_id)
            self.initial_observations[veh_id]["speed"] = \
                self.traci_connection.vehicle.getSpeed(veh_id)

            # save the initial state. This is used in the _reset function
            route_id = self.traci_connection.vehicle.getRouteID(veh_id)
            pos = self.traci_connection.vehicle.getPosition(veh_id)

            self.initial_state[veh_id] = \
                (self.initial_observations[veh_id]["type"], route_id,
                 self.initial_observations[veh_id]["lane"],
                 self.initial_observations[veh_id]["position"],
                 self.initial_observations[veh_id]["speed"], pos)

        # collect subscription information from sumo
        vehicle_obs = self.traci_connection.vehicle.getSubscriptionResults()
        tls_obs = self.traci_connection.trafficlights.getSubscriptionResults()
        id_lists = {tc.VAR_DEPARTED_VEHICLES_IDS: [],
                    tc.VAR_TELEPORT_STARTING_VEHICLES_IDS: [],
                    tc.VAR_ARRIVED_VEHICLES_IDS: []}

        # store new observations in the vehicles and traffic lights class
        self.vehicles.update(vehicle_obs, id_lists, self)
        self.traffic_lights.update(tls_obs)

        # store the network observations in the vehicles class
        self.vehicles.update(vehicle_obs, id_lists, self)

    def _step(self, rl_actions):
        """
        Run one timestep of the environment's dynamics. An autonomous agent
        (i.e. autonomous vehicles) performs an action provided by the RL
        algorithm. Other cars step forward based on their car following model.
        When end of episode is reached, reset() should be called to reset the
        environment's initial state.

        Parameters
        ----------
        rl_actions: numpy ndarray
            an list of actions provided by the rl algorithm

        Returns
        -------
        observation: numpy ndarray
            agent's observation of the current environment
        reward: float
            amount of reward associated with the previous state/action pair
        done: boolean
            indicates whether the episode has ended
        info: dictionary
            contains other diagnostic information from the previous action
        """
        self.time_counter += 1
        self.step_counter += 1
        if self.step_counter > 2e6:
            self.step_counter = 0
            self.restart_sumo(self.sumo_params, self.sumo_params.sumo_binary)
            
        # perform acceleration actions for controlled human-driven vehicles
        if len(self.vehicles.get_controlled_ids()) > 0:
            accel = []
            for veh_id in self.vehicles.get_controlled_ids():
                accel_contr = self.vehicles.get_acc_controller(veh_id)
                action = accel_contr.get_action(self)
                accel.append(action)
            self.apply_acceleration(self.vehicles.get_controlled_ids(), accel)

        # perform lane change actions for controlled human-driven vehicles
        if len(self.vehicles.get_controlled_lc_ids()) > 0:
            new_lane = []
            for veh_id in self.vehicles.get_controlled_lc_ids():
                lc_contr = self.vehicles.get_lane_changing_controller(veh_id)
                target_lane = lc_contr.get_action(self)
                new_lane.append(target_lane)
            self.apply_lane_change(self.vehicles.get_controlled_lc_ids(),
                                   target_lane=new_lane)

        # perform (optionally) routing actions for all vehicle in the network,
        # including rl and sumo-controlled vehicles
        routing_ids = []
        routing_actions = []
        for veh_id in self.vehicles.get_ids():
            if self.vehicles.get_routing_controller(veh_id) is not None:
                routing_ids.append(veh_id)
                route_contr = self.vehicles.get_routing_controller(veh_id)
                routing_actions.append(route_contr.choose_route(self))

        self.choose_routes(veh_ids=routing_ids, route_choices=routing_actions)

        self.apply_rl_actions(rl_actions)

        self.additional_command()

        self.traci_connection.simulationStep()

        # collect subscription information from sumo
        vehicle_obs = self.traci_connection.vehicle.getSubscriptionResults()
        id_lists = self.traci_connection.simulation.getSubscriptionResults()
        tls_obs = self.traci_connection.trafficlights.getSubscriptionResults()

        # store new observations in the vehicles and traffic lights class
        self.vehicles.update(vehicle_obs, id_lists, self)
        self.traffic_lights.update(tls_obs)

        # collect list of sorted vehicle ids
        self.sorted_ids, self.sorted_extra_data = self.sort_by_position()

        # collect information of the state of the network based on the
        # environment class used
        if isinstance(self.action_space, list):
            # rllab requires non-multi agent to have state shape as
            # num-states x num_vehicles
            self.state = self.get_state()
        else:
            self.state = self.get_state().T

        # collect observation new state associated with action
        next_observation = list(self.state)

        # crash encodes whether sumo experienced a crash
        crash = \
            self.traci_connection.simulation.getStartingTeleportNumber() != 0

        # compute the reward
        reward = self.compute_reward(self.state, rl_actions, fail=crash)

        # Are we in an rllab multi-agent scenario? If so, the action space is
        # a list.
        if isinstance(self.action_space, list):
            done_n = self.vehicles.num_rl_vehicles * [0]
            info_n = {'n': []}

            if self.shared_reward:
                info_n['reward_n'] = [reward] * len(self.action_space)
            else:
                info_n['reward_n'] = reward

            if crash:
                done_n = self.vehicles.num_rl_vehicles * [1]

            info_n['done_n'] = done_n
            info_n['state'] = self.state
            done = np.all(done_n)
            return self.state, sum(reward), done, info_n

        else:
            if crash:
                return next_observation, reward, True, {}
            else:
                return next_observation, reward, False, {}

    def _reset(self):
        """
        Resets the state of the environment, and re-initializes the vehicles in
        their starting positions. In "vehicle_arrangement_shuffle" is set to
        True in env_params, the vehicles swap initial positions with one
        another. Also, if a "starting_position_shuffle" is set to True, the
        initial position of vehicles is offset by some value.

        Returns
        -------
        observation: numpy ndarray
            the initial observation of the space. The initial reward is assumed
            to be zero.
        """
        # reset the time counter
        self.time_counter = 0
        self.next_period = 0
        if self.step_counter > 2e6:
            self.step_counter = 0
            self.restart_sumo(self.sumo_params, self.sumo_params.sumo_binary)

        # TODO(ak): handling number of vehicles during reset

        # create the list of colors used to visually distinguish between
        # different types of vehicles
        key_index = 1
        color_choice = np.random.choice(len(COLORS))
        for i in range(self.vehicles.num_types):
            self.colors[self.vehicles.types[i][0]] = \
                COLORS[(color_choice + key_index) % len(COLORS)]
            key_index += 1

        # perform shuffling (if requested)
        if self.starting_position_shuffle or self.vehicle_arrangement_shuffle:
            if self.starting_position_shuffle:
                x0 = np.random.uniform(0, self.scenario.length)
            else:
                x0 = self.scenario.initial_config.x0

            veh_ids = deepcopy(self.initial_ids)
            if self.vehicle_arrangement_shuffle:
                random.shuffle(veh_ids)

            initial_positions, initial_lanes = \
                self.scenario.generate_starting_positions(
                    num_vehicles=len(self.initial_ids), x0=x0)

            initial_state = dict()
            for i, veh_id in enumerate(veh_ids):
                route_id = "route" + initial_positions[i][0]

                # replace initial routes, lanes, and positions to reflect
                # new values
                list_initial_state = list(self.initial_state[veh_id])
                list_initial_state[1] = route_id
                list_initial_state[2] = initial_lanes[i]
                list_initial_state[3] = initial_positions[i][1]
                initial_state[veh_id] = tuple(list_initial_state)

                # replace initial positions in initial observations
                self.initial_observations[veh_id]["edge"] = \
                    initial_positions[i][0]
                self.initial_observations[veh_id]["position"] = \
                    initial_positions[i][1]

            self.initial_state = deepcopy(initial_state)

        # # clear all vehicles from the network and the vehicles class

        for veh_id in self.traci_connection.vehicle.getIDList():
            try:
                self.traci_connection.vehicle.remove(veh_id)
                self.traci_connection.vehicle.unsubscribe(veh_id)  # TODO(ak): add to master
                self.vehicles.remove(veh_id)
                # self.traci_connection.vehicle.unsubscribe(veh_id)
            except Exception:
                print("Error during start: {}".format(traceback.format_exc()))
                pass

        # clear all vehicles from the network and the vehicles class
        # FIXME (ev, ak) this is weird and shouldn't be necessary
        for veh_id in list(self.vehicles.get_ids()):
            self.vehicles.remove(veh_id)
            try:
                self.traci_connection.vehicle.remove(veh_id)
            except Exception:
                print("Error during start: {}".format(traceback.format_exc()))

        # reintroduce the initial vehicles to the network
        for veh_id in self.initial_ids:
            type_id, route_id, lane_index, lane_pos, speed, pos = \
                self.initial_state[veh_id]

            try:
                self.traci_connection.vehicle.addFull(
                    veh_id, route_id, typeID=str(type_id),
                    departLane=str(lane_index),
                    departPos=str(lane_pos), departSpeed=str(speed))
            except:
                # if a vehicle was not removed in the first attempt, remove it
                # now and then reintroduce it
                self.traci_connection.vehicle.remove(veh_id)
                self.traci_connection.vehicle.addFull(
                    veh_id, route_id, typeID=str(type_id),
                    departLane=str(lane_index),
                    departPos=str(lane_pos), departSpeed=str(speed))


        self.traci_connection.simulationStep()

        # collect subscription information from sumo
        vehicle_obs = self.traci_connection.vehicle.getSubscriptionResults()
        id_lists = self.traci_connection.simulation.getSubscriptionResults()
        tls_obs = self.traci_connection.trafficlights.getSubscriptionResults()

        # store new observations in the vehicles and traffic lights class
        self.vehicles.update(vehicle_obs, id_lists, self)
        self.traffic_lights.update(tls_obs)

        self.prev_last_lc = dict()
        for veh_id in self.vehicles.get_ids():
            # re-initialize the vehicles class with the states of the vehicles
            # at the start of a rollout
            self.vehicles.set_absolute_position(veh_id,
                                                self.get_x_by_id(veh_id))

            # re-initialize memory on last lc
            self.prev_last_lc[veh_id] = -1 * self.lane_change_duration

        # collect list of sorted vehicle ids
        self.sorted_ids, self.sorted_extra_data = self.sort_by_position()

        if isinstance(self.action_space, list):
            self.state = self.get_state()
        else:
            self.state = self.get_state().T

        observation = list(self.state)
        return observation

    def additional_command(self):
        """
        Additional commands that may be performed before a simulation step.
        """
        pass

    def apply_rl_actions(self, rl_actions):
        """
        Specifies the actions to be performed by rl_vehicles

        Parameters
        ----------
        rl_actions: numpy ndarray
            list of actions provided by the RL algorithm
        """
        pass

    def apply_acceleration(self, veh_ids, acc):
        """
        Applies the acceleration requested by a vehicle in sumo. Note that, if
        the sumo-specified speed mode of the vehicle is not "aggressive", the
        acceleration may be clipped by some safety velocity or maximum possible
        acceleration.

        Parameters
        ----------
        veh_ids: list of strings
            vehicles IDs associated with the requested accelerations
        acc: numpy array or list of float
            requested accelerations from the vehicles
        """
        for i, vid in enumerate(veh_ids):
            this_vel = self.vehicles.get_speed(vid)
            next_vel = max([this_vel + acc[i]*self.sim_step, 0])
            self.traci_connection.vehicle.slowDown(vid, next_vel, 1)

    def apply_lane_change(self, veh_ids, direction=None, target_lane=None):
        """
        Applies an instantaneous lane-change to a set of vehicles, while
        preventing vehicles from moving to lanes that do not exist.

        Parameters
        ----------
        veh_ids: list of strings
            vehicles IDs associated with the requested accelerations
        direction: list of int (-1, 0, or 1), optional
            -1: lane change to the right
             0: no lane change
             1: lane change to the left
        target_lane: list of int, optional
            lane indices the vehicles should lane-change to in the next step

        Raises
        ------
        ValueError
            If either both or none of "direction" and "target_lane" are provided
            as inputs. Only one should be provided at a time.
        ValueError
            If any of the direction values are not -1, 0, or 1.
        """
        if direction is not None and target_lane is not None:
            raise ValueError("Cannot provide both a direction and target_lane.")
        elif direction is None and target_lane is None:
            raise ValueError("A direction or target_lane must be specified.")

        current_lane = np.array(self.vehicles.get_lane(veh_ids))

        # if the direction is given, compute the target lane for vehicles
        if target_lane is None:
            # if any of the directions are not -1, 0, or 1, raise a ValueError
            if np.any(np.sign(direction) != np.array(direction)):
                raise ValueError("Direction values for lane changes may only "
                                 "be: -1, 0, or 1.")

            target_lane = current_lane + np.array(direction)

        for i, veh_id in enumerate(veh_ids):

            this_edge = self.vehicles.get_edge(veh_id)

            # check for multiple lanes
            if self.scenario.num_lanes(this_edge) == 1:
                continue

            target_lane[i] = min(
                max(target_lane[i], 0), self.scenario.num_lanes(this_edge) - 1)

            if target_lane[i] != current_lane[i]:
                self.traci_connection.vehicle.changeLane(
                    veh_id, int(target_lane[i]), 100000)

                if veh_id in self.vehicles.get_rl_ids():
                    self.prev_last_lc[veh_id] = \
                        self.vehicles.get_state(veh_id, "last_lc")

    def choose_routes(self, veh_ids, route_choices):
        """
        Updates the route choice of vehicles in the network.

        Parameters
        ----------
        veh_ids: list
            list of vehicle identifiers
        route_choices: numpy array or list of floats
            list of edges the vehicle wishes to traverse, starting with the edge
            the vehicle is currently on. If a value of None is provided, the
            vehicle does not update its route
        """
        for i, veh_id in enumerate(veh_ids):
            if route_choices[i] is not None:
                self.traci_connection.vehicle.setRoute(
                    vehID=veh_id, edgeList=route_choices[i])

    def get_x_by_id(self, veh_id):
        """
        Provides a 1-dimensional representation of the position of a vehicle
        in the network.

        Parameters
        ----------
        veh_id: string
            vehicle identifier

        Yields
        ------
        float
            position of a vehicle relative to a certain reference.
        """
        if self.vehicles.get_edge(veh_id) == '':
            # occurs when a vehicle crashes is teleported for some other reason
            return 0.
        return self.scenario.get_x(self.vehicles.get_edge(veh_id),
                                   self.vehicles.get_position(veh_id))

    def sort_by_position(self):
        """Sorts the vehicle ids of vehicles in the network by position. The
        base environment does this by sorting vehicles by their absolute
        position.

        Returns
        -------
        sorted_ids: list <str>
            a list of all vehicle IDs sorted by position
        sorted_extra_data: list or tuple
            an extra component (list, tuple, etc...) containing extra sorted
            data, such as positions. If no extra component is needed, a value
            of None should be returned
        """
        if self.env_params.sort_vehicles:
            sorted_ids = sorted(self.vehicles.get_ids(),
                                key=self.vehicles.get_absolute_position)
            return sorted_ids, None
        else:
            return self.vehicles.get_ids(), None

    def get_state(self):
        """
        Returns the state of the simulation as perceived by the learning agent.
        MUST BE implemented in new environments.

        Returns
        -------
        state: numpy ndarray
            information on the state of the vehicles, which is provided to the
            agent
        """
        raise NotImplementedError

    @property
    def action_space(self):
        """
        Identifies the dimensions and bounds of the action space (needed for
        gym environments).
        MUST BE implemented in new environments.

        Yields
        -------
        gym Box or Tuple type
            a bounded box depicting the shape and bounds of the action space
        """
        raise NotImplementedError

    @property
    def observation_space(self):
        """
        Identifies the dimensions and bounds of the observation space (needed
        for gym environments).
        MUST BE implemented in new environments.

        Yields
        -------
        gym Box or Tuple type
            a bounded box depicting the shape and bounds of the observation
            space
        """
        raise NotImplementedError

    def compute_reward(self, state, rl_actions, **kwargs):
        """
        Reward function for RL.
        MUST BE implemented in new environments.
        Defaults to 0 for non-implemented environments.

        Parameters
        ----------
        state: numpy ndarray
            state of all the vehicles in the simulation
        rl_actions: numpy ndarray
            actions performed by rl vehicles
        kwargs: dictionary
            other parameters of interest. Contains a "fail" element, which
            is True if a vehicle crashed, and False otherwise

        Returns
        -------
        reward: float or list <float>
        """
        return 0

    def terminate(self):
        """
        Closes the TraCI I/O connection. Should be done at end of every
        experiment. Must be in Environment because the environment opens the
        TraCI connection.
        """
        self._close()

    def _close(self):
        self.traci_connection.close()

    def teardown_sumo(self):
        try:
            os.killpg(self.sumo_proc.pid, signal.SIGTERM)
        except Exception:
            print("Error during teardown: {}".format(traceback.format_exc()))

    def _seed(self, seed=None):
        return []
