"""Contains the scenario class for .net.xml files."""

from flow.core.params import InitialConfig
from flow.core.traffic_lights import TrafficLights
from flow.scenarios.base_scenario import Scenario

from lxml import etree
import xml.etree.ElementTree as ElementTree
import re


class NetFileScenario(Scenario):
    """Class that creates a scenario from a .net.xml file.

    The .net.xml file is specified in the NetParams object. For example:

        >>> from flow.core.params import NetParams
        >>> net_params = NetParams(netfile="/path/to/netfile.net.xml")

    No "specify_nodes" and "specify_edges" routes are needed. However, a
    "specify_routes" file is still needed to specify the appropriate routes
    vehicles can traverse in the network.
    """

    def __init__(self,
                 name,
                 vehicles,
                 net_params,
                 initial_config=InitialConfig(),
                 traffic_lights=TrafficLights()):
        """Initialize a scenario from a .net.xml file.

        See flow/scenarios/base_scenario.py for description of params.
        """
        self.vehicle_data, self.type_data = self.vehicle_infos(["/Users/umang/Downloads/LuSTScenario/scenario/DUARoutes/local.0.rou.xml",
                                                                "/Users/umang/Downloads/LuSTScenario/scenario/DUARoutes/local.1.rou.xml",
                                                                "/Users/umang/Downloads/LuSTScenario/scenario/DUARoutes/local.2.rou.xml"])

        for t in self.type_data:
            vehicles.add(t, num_vehicles=0, lane_change_mode=1621)

        super().__init__(name, vehicles, net_params,
                         initial_config, traffic_lights)

    def vehicle_infos(self,filenames):
        """Import of vehicle from a configuration file.

        This is a utility function for computing vehicle information. It imports a
        network configuration file, and returns the information on the vehicle and add it into the Vehicle object

        Parameters
        ----------
        filename : str type
        path to the xml file to load

        Returns
        -------
        Flow Vehicle object
        vehicle_data : dict <dict>
        Key = id of the vehicle
        Element = dict of departure speed, vehicle type, depart Position, depart edges

        """

        vehicle_data = dict()
        type_data = dict()

        for filename in filenames:
            # import the .net.xml file containing all edge/type data
            parser = etree.XMLParser(recover=True)
            tree = ElementTree.parse(filename, parser=parser)

            root = tree.getroot()
            for vehicle in root.findall('vehicle'):

                id_vehicle=vehicle.attrib['id']
                departSpeed=vehicle.attrib['departSpeed']
                depart=vehicle.attrib['depart']
                type_vehicle=vehicle.attrib['type']
                departPos=vehicle.attrib['departPos']
                depart_edges=vehicle.findall('route')[0].attrib["edges"].split(' ')[0]

                routes_data = dict()
                for route in vehicle.findall('route'):
                    route_edges = route.attrib["edges"].split(' ')
                    #key=route_edges[0]
                    # if key in routes_data.keys():
                    #     for routes in routes_data[key]:
                    #         if routes==route_edges:
                    #             pass
                    #         else:   
                    #             routes_data[key].append(route_edges)
                    # else:
                    #routes_data[key] = [route_edges]

                if type_vehicle not in type_data:
                    type_data[type_vehicle] = 1
                else:
                    type_data[type_vehicle] += 1

                vehicle_data[id_vehicle]={'departSpeed':departSpeed,'route_edges': route_edges, 'depart':depart,'typeID':type_vehicle,'departPos':departPos, "vehID": id_vehicle}

        return vehicle_data, type_data

    def specify_edge_starts(self):
        """See parent class.

        The edge starts are specified from the network configuration file. Note
        that, the values are arbitrary but do not allow the positions of any
        two edges to overlap, thereby making them compatible with all starting
        position methods for vehicles.
        """
        # the total length of the network is defined within this function
        self.length = 0

        edgestarts = []
        for edge_id in self._edge_list:
            # the current edge starts (in 1D position) where the last edge
            # ended
            edgestarts.append((edge_id, self.length))
            # increment the total length of the network with the length of the
            # current edge
            self.length += self._edges[edge_id]["length"]

        return edgestarts

    def specify_internal_edge_starts(self):
        """See parent class.

        All internal edge starts are given a position of -1. This may be
        overridden; however, in general we do not worry about internal edges
        and junctions in large networks.
        """
        return [(":", -1)]

    def close(self):
        """See parent class.

        The close method is overwritten here because we do not want Flow to
        delete externally designed networks.
        """
        pass

    def generate_net(self, net_params, traffic_lights):
        """See parent class.

        The network file is generated from the .osm file specified in
        net_params.osm_path
        """
        # name of the .net.xml file (located in cfg_path)
        self.netfn = "/Users/umang/Downloads/LuSTScenario/scenario/lust.net.xml"
        # self.netfn = net_params.netfile

        # collect data from the generated network configuration file
        edges_dict, conn_dict = self._import_edges_from_net()

        self.edge_dict = edges_dict
        return edges_dict, conn_dict

    def specify_nodes(self, net_params):
        """See class definition."""
        pass

    def specify_edges(self, net_params):
        """See class definition."""
        pass

    def specify_types(self, net_params):
        types = self.vehicle_type("/Users/umang/Downloads/LuSTScenario/scenario/vtypes.add.xml")
        return types

    def _import_routes_from_net(self, filename):
        """Import route from a configuration file.

        This is a utility function for computing route information. It imports a
        network configuration file, and returns the information on the routes
        taken by all the vehicle located in the file.

        Parameters
        ----------
        filename : str type
        path to the xml file to load

        Returns
        -------
        routes_data : dict <dict>
        Key = name of the first route taken
        Element = list of all the routes taken by the vehicle starting in that route

        """
        # # import the .net.xml file containing all edge/type data
        # parser = etree.XMLParser(recover=True)
        # tree = ElementTree.parse(filename, parser=parser)

        # root = tree.getroot()

        # # Collect information on the available types (if any are available).
        # # This may be used when specifying some route data.
        # routes_data = dict()

        # for vehicle in root.findall('vehicle'):
        #     for route in vehicle.findall('route'):
        #         route_edges = route.attrib["edges"].split(' ')
        #         key=route_edges[0]
        #         if key in routes_data.keys():
        #             for routes in routes_data[key]:
        #                 if routes==route_edges:
        #                     pass
        #                 else:
        #                     routes_data[key].append(route_edges)
        #         else:
        #             routes_data[key] = [route_edges]
        # return routes_data

        # Crystal's version of extracting routes
        # I didn't test this myself and I don't guarantee correctness in this
        # (completeness or uniqueness of routes), but here it is.
        # Use for faster performance and be sure to test it.

        with open(filename, 'r') as myfile:
            data = myfile.read()

            # regex to extract routes
            pattern = '<route edges=".*?"\/>' # Extracts <route edges="..."/>
            matches = re.findall(pattern, data)

            # Strip <route edges>
            for i in range(len(matches)):
                matches[i] = matches[i].split('"')[1]

            # Create each as list and add to set
            distinct_routes = set()
            for i in range(len(matches)):
                curr_route = tuple(matches[i].split(' '))
                matches[i] = curr_route
                distinct_routes.add(curr_route)

            # Compare length of all routes (matches) vs length of distinct routes
            #print(len(matches), len(distinct_routes))

            routes_data = {}
            for route in distinct_routes:
                first_edge = route[0]
                if first_edge in routes_data:

                    ### We need to figure out that part
                    pass
                    #routes_data[first_edge].append(list(route))
                else:

                    routes_data[first_edge] = list(route)
                    #routes_data[first_edge] = [list(route)]

            # Print first item in edge-routes dictionary
            #print(list(routes_data.items())[0])

            return routes_data

    def specify_routes(self, net_params):
        """ Format all the routes from the xml file
        Parameters
        ----------
        filename : str type
        path to the rou.xml file to load
        Returns
        -------
        routes_data : dict <dict>
        Key = name of the first route taken
        Element = list of all the routes taken by the vehicle starting in that route
        """
        # routes_data={}
        # for edge in self.edge_dict:
        #     if ':' not in edge:
        #         routes_data[edge]=[edge]
        #routes_data = self._import_routes_from_net("/Users/lucasfischer/sumo/LuSTScenario/scenario/DUARoutes/local.1.rou.xml")

        routes_data = {}
        for t in self.vehicle_data:
            routes_data[t] = self.vehicle_data[t]["route_edges"]

        return routes_data

    def _import_tls_from_net(self,filename):
        """Import traffic lights from a configuration file.
        This is a utility function for computing traffic light information. It imports a
        network configuration file, and returns the information of the traffic lights in the file.
        Parameters
        ----------
        filename : str type
        path to the rou.xml file to load
        Returns
        -------
        tl_logic : TrafficLights
        """
        # import the .net.xml file containing all edge/type data
        parser = etree.XMLParser(recover=True)
        tree = ElementTree.parse(filename, parser=parser)
        root = tree.getroot()
        # create TrafficLights() class object to store traffic lights information from the file
        tl_logic = TrafficLights()
        for tl in root.findall('tlLogic'):
            phases = [phase.attrib for phase in tl.findall('phase')]
            tl_logic.add(tl.attrib['id'], tl.attrib['type'], tl.attrib['programID'], tl.attrib['offset'], phases)
        return tl_logic

    def vehicle_type(self,filename):
        """Import vehicle type from an vtypes.add.xml file .
        This is a utility function for outputting all the type of vehicle . .
        Parameters
        ----------
        filename : str type
        path to the vtypes.add.xml file to load
        Returns
        -------
        dict: the key is the vehicle_type id and the value is a dict we've type of the vehicle, depart edges , depart Speed, departPos
        """
        parser = etree.XMLParser(recover=True)
        tree = ElementTree.parse(filename, parser=parser)

        root = tree.getroot()
        veh_type = {}

        for transport in root.findall('vTypeDistribution'):
            for vtype in transport.findall('vType'):
                vClass=vtype.attrib['vClass']
                id_type_vehicle=vtype.attrib['id']
                accel=vtype.attrib['accel']
                decel=vtype.attrib['decel']
                sigma=vtype.attrib['sigma']
                length=vtype.attrib['length']
                minGap=vtype.attrib['minGap']
                maxSpeed=vtype.attrib['maxSpeed']
                probability=vtype.attrib['probability']
                speedDev=vtype.attrib['speedDev']
                veh_type[id_type_vehicle]={'vClass':vClass,'accel':accel,'decel':decel,'sigma':sigma,'length':length,'minGap':minGap,'maxSpeed':maxSpeed,'probability':probability,'speedDev':speedDev}
        return veh_type

    def gen_custom_start_pos(self, initial_config, num_vehicles, **kwargs):
        """Generate a user defined set of starting positions.
        This method is just used for testing.
        Parameters
        ----------
        initial_config : InitialConfig type
        see flow/core/params.py
        num_vehicles : int
        number of vehicles to be placed on the network
        kwargs : dict
        extra components, usually defined during reset to overwrite initial
        config parameters
        Returns
        -------
        startpositions : list of tuple (float, float)
        list of start positions [(edge0, pos0), (edge1, pos1), ...]
        startlanes : list of int
        list of start lanes
        """
        return kwargs["start_positions"], kwargs["start_lanes"]
