from argparse import ArgumentParser
import h5py, json, copy, importlib
import numpy as np
from enum import Enum
import random
import stopit
from typing import List, Dict, Tuple
from tdw.tdw_utils import TDWUtils
from tdw.librarian import ModelRecord, MaterialLibrarian
from tdw_physics.rigidbodies_dataset import (RigidbodiesDataset,
                                             get_random_xyz_transform,
                                             get_range,
                                             handle_random_transform_args)
from tdw_physics.util import (MODEL_LIBRARIES,
                              get_parser,
                              xyz_to_arr,
                              arr_to_xyz,
                              str_to_xyz)
from tdw_physics.target_controllers.dominoes import (get_args,
                                                     none_or_str,
                                                     none_or_int)
from tdw_physics.target_controllers.playroom import Playroom

# postproc
from tdw_physics.postprocessing.labels import (stimulus_name,
                                               get_static_val,
                                               object_visible_area,
                                               is_any_object_fully_occluded,
                                               get_collisions)


MODEL_NAMES = [r.name for r in MODEL_LIBRARIES['models_full.json'].records if not r.do_not_use]
PRIMITIVE_NAMES = [r.name for r in MODEL_LIBRARIES['models_flex.json'].records if not r.do_not_use]
SPECIAL_NAMES =[r.name for r in MODEL_LIBRARIES['models_special.json'].records if not r.do_not_use]
ALL_NAMES = MODEL_NAMES + SPECIAL_NAMES + PRIMITIVE_NAMES

M = MaterialLibrarian()
MATERIAL_TYPES = M.get_material_types()
MATERIAL_NAMES = {mtype: [m.name for m in M.get_all_materials_of_type(mtype)] \
                  for mtype in MATERIAL_TYPES}

## convenience
XYZ = ['x', 'y', 'z']

## Relation types
class Relation(Enum):
    contain = 'contain'
    support = 'support'
    occlude = 'occlude'
    null = 'null'

    def __str__(self):
        return self.value


def get_relational_args(dataset_dir: str, parse=True):

    common = get_parser(dataset_dir, get_help=False)
    domino, domino_postproc = get_args(dataset_dir, parse=False)
    parser = ArgumentParser(parents=[common, domino], conflict_handler='resolve',
                            fromfile_prefix_chars='@')

    ## Relation type
    parser.add_argument("--relation",
                        type=str,
                        default=','.join([r.name for r in Relation]),
                        help="Which relation type to construct")

    ## scenarios
    parser.add_argument("--start",
                        type=none_or_int,
                        default=0,
                        help="which scenario to start with")
    parser.add_argument("--end",
                        type=none_or_int,
                        default=None,
                        help="which scenario to end on (exclusive)")
    parser.add_argument("--training_data",
                        type=none_or_int,
                        default=1,
                        help="Whether to use training data or testing data")
    
    ## Object types
    parser.add_argument("--container",
                        type=none_or_str,
                        default="b04_bowl_smooth",
                        help="comma-separated list of container names")
    parser.add_argument("--target",
                        type=none_or_str,
                        default="b04_clownfish",
                        help="comma-separated list of target object names")
    parser.add_argument("--distractor",
                        type=none_or_str,
                        default="b05_lobster",
                        help="comma-separated list of distractor object names")

    ## Object positions
    parser.add_argument("--cposition",
                        type=str,
                        default="[[-0.25,0.25],0.0,[-0.25,0.25]]",
                        help="Position ranges for the container")
    parser.add_argument("--tposition",
                        type=str,
                        default="[0.3,1.0]",
                        help="Position ranges for the target/distractor (offset from container)")
    parser.add_argument("--trotation",
                        type=str,
                        default="[0,180]",
                        help="Pose ranges for the target/distractor")
    parser.add_argument("--thorizontal",
                        action="store_true",
                        help="Whether to rotate the target object so that it's horizontal")
    parser.add_argument("--tangle",
                        type=str,
                        default="[0,30]",
                        help="How much to jitter the target position angle relative to container")
    parser.add_argument("--tjitter",
                        type=float,
                        default="0.2",
                        help="How much to jitter the target position")

    ## Object scales
    parser.add_argument("--cscale",
                        type=str,
                        default="[0.75,1.25]",
                        help="scale of container")
    parser.add_argument("--tscale",
                        type=str,
                        default="[1.0,1.5]",
                        help="scale of target object")
    parser.add_argument("--max_target_scale_ratio",
                        type=float,
                        default=0.9,
                        help="Max ratio between target and container scale")
    parser.add_argument("--dscale",
                        type=str,
                        default="[0.75,1.25]",
                        help="scale of distractor")

    ## Camera
    parser.add_argument("--camera_distance",
                        type=none_or_str,
                        default="[1.75,2.5]",
                        help="radial distance from camera to centerpoint")
    parser.add_argument("--camera_min_height",
                        type=float,
                        default=1.25,
                         help="min height of camera")
    parser.add_argument("--camera_max_height",
                        type=float,
                        default=2.0,
                        help="max height of camera")
    parser.add_argument("--camera_min_angle",
                        type=float,
                        default=0,
                        help="minimum angle of camera rotation around centerpoint")
    parser.add_argument("--camera_max_angle",
                        type=float,
                        default=360,
                        help="maximum angle of camera rotation around centerpoint")
    parser.add_argument("--camera_left_right_reflections",
                        action="store_true",
                        help="Whether camera angle range includes reflections along the collision axis")

    ## Changed defaults
    parser.add_argument("--room",
                        type=str,
                        default="tdw",
                        help="Which room to be in")
    parser.add_argument("--zscale",
                        type=str,
                        default="-1.0",
                        help="scale of target zone")
    parser.add_argument("--zcolor",
                        type=none_or_str,
                        default=None,
                        help="comma-separated R,G,B values for the target zone color. None is random")
    parser.add_argument("--zmaterial",
                        type=none_or_str,
                        default=None,
                        help="Material name for zone. If None, samples from material_type")
    parser.add_argument("--material_types",
                        type=none_or_str,
                        default="Wood,Metal,Ceramic",
                        help="Which class of materials to sample material names from")



    def postprocess(args):

        args.relation = [r for r in Relation if r.name in args.relation.split(',')]

        args.container = [nm for nm in args.container.split(',') if nm in ALL_NAMES]
        args.target = [nm for nm in args.target.split(',') if nm in ALL_NAMES]
        args.distractor = [nm for nm in args.distractor.split(',') if nm in ALL_NAMES]

        args.zscale = handle_random_transform_args(args.zscale)
        args.cscale = handle_random_transform_args(args.cscale)
        args.tscale = handle_random_transform_args(args.tscale)
        args.dscale = handle_random_transform_args(args.dscale)

        args.cposition = handle_random_transform_args(args.cposition)
        args.cposition["y"] = 0.0

        args.tposition = handle_random_transform_args(args.tposition)
        args.trotation = handle_random_transform_args(args.trotation)
        args.tangle = handle_random_transform_args(args.tangle)

        args.camera_distance = handle_random_transform_args(args.camera_distance)

        return args

    args = parser.parse_args()
    args = postprocess(args)

    return args

## postprocessing
def container_visible_area(d):
    return object_visible_area(d, 'container_id', frame_num=-1)
def target_visible_area(d):
    return object_visible_area(d, 'target_id', frame_num=-1)
def distractor_visible_area(d):
    return object_visible_area(d, 'distractor_id', frame_num=-1)
def is_target_visible(d, thresh=0.01):
    return bool(target_visible_area(d) > thresh)
def is_distractor_visible(d, thresh=0.01):
    return bool(distractor_visible_area(d) > thresh)
def is_container_visible(d, thresh=0.01):
    return bool(container_visible_area(d) > thresh)
def all_visible(d, thresh=0.01):
    return all((f(d, thresh) for f in [is_target_visible, is_distractor_visible, is_container_visible]))

def target_not_touching_ground(d, frame_num=-1):
    '''Checks whether the target is container or supported'''
    enco = list(get_collisions(d, frame_num, env_collisions=True)['object_ids'])
    coll = list(get_collisions(d, frame_num, env_collisions=False)['object_ids'])
    coll = [list(c) for c in coll]
    target_id = get_static_val(d, 'target_id')
    container_id = get_static_val(d, 'container_id')

    target_on_ground = target_id in enco
    target_on_container = ([target_id, container_id] in coll) or ([container_id, target_id] in coll)
    return bool((not target_on_ground))
    

## controller
class RelationArrangement(Playroom):

    PRINT = False

    def __init__(self, port=1071,
                 relation=list(Relation),
                 container=PRIMITIVE_NAMES,
                 target=PRIMITIVE_NAMES,
                 distractor=PRIMITIVE_NAMES,
                 container_position_range=[-0.25,0.25],
                 container_scale_range=[1.0,1.5],
                 target_position_range=[0.25,1.0],
                 target_rotation_range=[0,180],
                 target_always_horizontal=False,
                 target_angle_range=[-30,30],
                 target_scale_range=[1.0,1.5],
                 target_position_jitter=0.25,
                 target_rotation_jitter=30,
                 distractor_scale_range=[1.0,1.5],
                 max_target_scale_ratio=0.8,
                 **kwargs):

        super().__init__(port=port, **kwargs)

        ## relation types
        self.set_relation_types(relation)
        print("relation types", [r.name for r in self._relation_types])

        ## how much larger target can be than the container
        self.max_target_scale_ratio = max_target_scale_ratio

        ## object types
        self.set_container_types(container)
        self.set_target_types(target)
        self.set_distractor_types(distractor)

        ## object positions
        self.container_position_range = container_position_range
        self.target_position_range = target_position_range
        self.target_rotation_range = target_rotation_range
        self.target_always_horizontal = target_always_horizontal
        self.target_angle_range = target_angle_range
        self.target_position_jitter = target_position_jitter
        self.target_rotation_jitter = target_rotation_jitter

        ## object scales
        self.container_scale_range = container_scale_range
        self.target_scale_range = target_scale_range
        self.distractor_scale_range = distractor_scale_range

        ## object textures
        self.container_material = None
        self.target_material = None
        self.distractor_material = None

        ## when to stop trial
        self.flow_thresh = 5.0
        self.min_frames = 90

        print("sampling containers from", [(r.name, r.wcategory) for r in self._container_types], len(self._container_types))
        print("sampling targets from", [(r.name, r.wcategory) for r in self._target_types], len(self._target_types))
        print("sampling distractors from", [(r.name, r.wcategory) for r in self._distractor_types], len(self._distractor_types))

    def is_done(self, resp: List[bytes], frame: int) -> bool:
        return ((frame > self.min_frames) and (self._max_optical_flow(resp) < self.flow_thresh))

    def _write_frame_labels(self, frame_grp, resp, frame_num, sleeping):
        return RigidbodiesDataset._write_frame_labels(self, frame_grp, resp, frame_num, sleeping)

    def set_relation_types(self, rlist):
        if not isinstance(rlist, list):
            rlist = [rlist]
        self._relation_types = [r for r in rlist if r in Relation]

    def set_types(self, olist):
        tlist = self.get_types(olist,
                               libraries=["models_full.json", "models_special.json", "models_flex.json"],
                               categories=None,
                               flex_only=False,
                               size_min=None, size_max=None)
        return tlist

    def set_container_types(self, olist):
        self._container_types = self.set_types(olist)

    def set_target_types(self, olist):
        self._target_types = self.set_types(olist)

    def set_distractor_types(self, olist):
        self._distractor_types = self.set_types(olist)

    def _write_static_data(self, static_group: h5py.Group) -> None:
        RigidbodiesDataset._write_static_data(self, static_group)

        # randomization
        try:
            static_group.create_dataset("room", data=self.room)
        except (AttributeError,TypeError):
            pass
        try:
            static_group.create_dataset("seed", data=self.seed)
        except (AttributeError,TypeError):
            pass
        try:
            static_group.create_dataset("randomize", data=self.randomize)
        except (AttributeError,TypeError):
            pass
        try:
            static_group.create_dataset("trial_seed", data=self.trial_seed)
        except (AttributeError,TypeError):
            pass
        try:
            static_group.create_dataset("trial_num", data=self._trial_num)
        except (AttributeError,TypeError):
            pass

        # models
        try:
            static_group.create_dataset("zone_id", data=self.zone_id)
        except (AttributeError,TypeError):
            pass
        try:
            static_group.create_dataset("target_id", data=self.target_id)
            static_group.create_dataset("target_name", data=self.target.name.encode('utf8'))
        except (AttributeError,TypeError):
            pass
        try:
            static_group.create_dataset("container_id", data=self.container_id)
            static_group.create_dataset("container_name", data=self.container.name.encode('utf8'))
        except (AttributeError,TypeError):
            pass
        try:
            static_group.create_dataset("distractor_id", data=self.distractor_id)
            static_group.create_dataset("distractor_name", data=self.container.name.encode('utf8'))
        except (AttributeError,TypeError):
            pass

    def _place_camera(self) -> List[dict]:
        commands = []
        a_pos = self.get_random_avatar_position(radius_min=self.camera_radius_range[0],
                                                radius_max=self.camera_radius_range[1],
                                                angle_min=self.camera_min_angle,
                                                angle_max=self.camera_max_angle,
                                                y_min=self.camera_min_height,
                                                y_max=self.camera_max_height,
                                                center=TDWUtils.VECTOR3_ZERO,
                                                reflections=self.camera_left_right_reflections)
        self._set_avatar_attributes(a_pos)
        commands.extend([
            {"$type": "teleport_avatar_to",
             "position": self.camera_position},
            {"$type": "look_at_position",
             "position": self.camera_aim},
            {"$type": "set_focus_distance",
             "focus_distance": TDWUtils.get_distance(a_pos, self.camera_aim)}
        ])

        return commands

    def _flip_container(self) -> None:
        self.container_flipped = True
        self.container_rotation.update({"z": 180})
        self.container_position["y"] += self.container_height

    def _place_container(self) -> List[dict]:
        '''
        TODO
        '''
        commands = []

        ## choose a relation type
        self.relation = random.choice(self._relation_types)

        ## create the container
        record, data = self.random_primitive(self._container_types,
                                             scale=1.0,
                                             color=None,
                                             add_data=False)
        self.container = record
        self.container_id = data["id"]

        ## scale the container so it's in the required size range
        self.container_scale = self.rescale_record_to_size(record, self.container_scale_range, randomize=True)
        _,cheight,_ = self.get_record_dimensions(self.container)
        self.container_height = cheight * self.container_scale["y"]

        ## jitter the xz position of the container
        self.container_position = get_random_xyz_transform(self.container_position_range)

        ## rotate the container in the xz plane
        self.container_rotation = self.get_y_rotation([0, 360])

        ## if relationship is not contain, [possibly] flip
        print("RELATION", self.relation.name)
        if self.relation == Relation.support:
            self._flip_container()
        elif self.relation == Relation.contain:
            self.container_flipped = False
        else:
            if random.choice([0,1]):
                self._flip_container()

        ## place the container
        add_container_cmds = self.add_primitive(
            record=self.container,
            position=self.container_position,
            rotation=self.container_rotation,
            scale=self.container_scale,
            material=self.container_material,
            color=data["color"],
            mass=5.0,
            scale_mass=True,
            o_id=self.container_id,
            add_data=True,
            make_kinematic=True,
            apply_texture=(True if self.container_material else False)
        )
        commands.extend(add_container_cmds)

        return commands

    def _choose_target_position(self) -> None:
        self.target_position = self.left_or_right = None
        theta = random.uniform(*get_range(self.target_angle_range)) * random.choice([-1.,1.])
        tx,ty,tz = [self.get_record_dimensions(self.target)[i] * self.target_scale[k] * 0.5
                    for i,k in enumerate(XYZ)]
        offset = max(tx, ty, tz)
        try:
            tpos = random.uniform(*get_range(self.target_position_range)) + offset
        except:
            tpos = random.uniform(*get_range(self.target_position_range['x'])) + offset

        ## if contain or support, place the target on the container;
        if (self.relation == Relation.support) or (self.relation == Relation.contain):
            self.target_position = copy.deepcopy(self.container_position)
            self.target_position["y"] = self.container_height

        ## elif occlude, put it mostly behind the container
        elif self.relation == Relation.occlude:
            unit_v = self.rotate_vector_parallel_to_floor(self.opposite_unit_vector, theta)
            self.target_position = {
                "x": unit_v["x"] * tpos,
                "y": self.container_height,
                "z": unit_v["z"] * tpos
            }

        ## elif null, put it to one side of the container
        elif self.relation == Relation.null:
            self.left_or_right = random.choice([-90, 90])
            unit_v = self.rotate_vector_parallel_to_floor(self.opposite_unit_vector, theta + self.left_or_right)
            self.target_position = {
                "x": unit_v["x"] * tpos,
                "y": self.container_height,
                "z": unit_v["z"] * tpos
            }

        else:
            raise NotImplementedError("You need to institute a rule for this relation type")

        ## jitter position
        for k in ["x", "z"]:
            self.target_position[k] += random.uniform(-self.target_position_jitter, self.target_position_jitter)

    def _choose_target_rotation(self) -> None:

        ## random pose in xz plane
        self.target_rotation = self.get_y_rotation(self.target_rotation_range)
        self.target_rotation["y"] += random.choice([0, 180])

        ## whether to make the target horizontal or not
        self.target_horizontal = False
        if self.target_always_horizontal or bool(random.choice([0,1])):
            self.target_horizontal = True
            sy = self.get_record_dimensions(self.target)[1] * self.target_scale["y"]
            self.target_rotation["z"] = random.choice([-90, 90])

            self.target_position["z"] += -np.sin(np.radians(self.target_rotation["y"])) * 0.5 * sy * np.sign(self.target_rotation["z"])
            self.target_position["x"] += np.cos(np.radians(self.target_rotation["y"])) * 0.5 * sy * np.sign(self.target_rotation["z"])


        if self.relation != Relation.support:
            self.target_rotation["z"] += random.uniform(-self.target_rotation_jitter, self.target_rotation_jitter)
        if self.target_horizontal:
            self.target_position["y"] += self.get_record_dimensions(self.target)[0] * self.target_scale["x"] * 0.5

    def _place_target_object(self) -> List[dict]:
        """
        Choose and place the target object as a function of relation type
        """
        commands = []

        ## create the target
        record, data = self.random_primitive(self._target_types,
                                             scale=1.0,
                                             color=None,
                                             add_data=False)
        self.target = record
        self.target_id = data["id"]

        ## rescale the target; make sure it's not much bigger than the container!
        _tscale_range = copy.deepcopy(self.target_scale_range)
        _cscale = self.container_scale
        _tscale_range = [
            min(_tscale_range[0], *[_cscale[k] for k in XYZ]) * self.max_target_scale_ratio,
            min(_tscale_range[1], *[_cscale[k] for k in XYZ]) * self.max_target_scale_ratio
        ]
        self.target_scale = self.rescale_record_to_size(record, _tscale_range, randomize=True)

        ## choose the target position as a function of relation type
        self._choose_target_position()

        ## chose the target pose
        self._choose_target_rotation()

        if self.PRINT:
            print("CONTAINER POS", self.container_position)
            print("TARGET POS", self.target_position)


        add_target_cmds = self.add_primitive(
            record=self.target,
            position=self.target_position,
            rotation=self.target_rotation,
            scale=self.target_scale,
            material=self.target_material,
            color=data["color"],
            mass=2.0,
            static_friction=1.0,
            dynamic_friction=1.0,
            bounciness=0.0,
            scale_mass=False,
            o_id=self.target_id,
            add_data=True,
            make_kinematic=False,
            apply_texture=(True if self.target_material else False)
        )
        commands.extend(add_target_cmds)

        return commands

    def _choose_distractor_position(self) -> None:
        self.distractor_position = None
        theta = random.uniform(*get_range(self.target_angle_range)) * random.choice([-1.,1.])
        dx,dy,dz = [self.get_record_dimensions(self.distractor)[i] * self.distractor_scale[k] * 0.5
                    for i,k in enumerate(XYZ)]
        offset = max(dx, dy, dz)
        try:
            dpos = random.uniform(*get_range(self.target_position_range)) + offset
        except:
            dpos = random.uniform(*get_range(self.target_position_range['x'])) + offset

        ## if relation is null, make sure distractor is on opposite side
        if self.left_or_right is None:
            l_or_r = random.choice([-90, 90])
        else:
            l_or_r = -self.left_or_right
        unit_v = self.rotate_vector_parallel_to_floor(self.opposite_unit_vector, theta + l_or_r)
        self.distractor_position = {
            "x": unit_v["x"] * dpos,
            "y": self.container_height,
            "z": unit_v["z"] * dpos
        }

        ## jitter position
        for k in ["x", "z"]:
            self.distractor_position[k] += random.uniform(-self.target_position_jitter, self.target_position_jitter)

    def _choose_distractor_rotation(self) -> None:

        ## random pose in xz plane
        self.distractor_rotation = self.get_y_rotation(self.target_rotation_range)
        self.distractor_rotation["y"] += random.choice([0, 180])

        ## whether to make the distractor horizontal or not
        self.distractor_horizontal = bool(random.choice([0,1]))
        if self.distractor_horizontal:
            dy = self.get_record_dimensions(self.distractor)[1] * self.distractor_scale["y"]
            self.distractor_rotation["z"] = random.choice([-90,90])
            self.distractor_position["z"] += -np.sin(np.radians(self.distractor_rotation["y"])) * 0.5 * dy * np.sign(self.distractor_rotation["z"])
            self.distractor_position["x"] += np.cos(np.radians(self.distractor_rotation["y"])) * 0.5 * dy * np.sign(self.distractor_rotation["z"])

            self.distractor_position["y"] += self.get_record_dimensions(self.distractor)[0] * self.distractor_scale["x"] * 0.5

        self.distractor_rotation["z"] += random.uniform(-self.target_rotation_jitter, self.target_rotation_jitter)

    def _place_distractor(self) -> List[dict]:
        '''
        Choose and place a distractor to the left or right of the scene.
        '''
        commands = []

        ## create the distractor
        record, data = self.random_primitive(self._distractor_types,
                                             scale=1.0,
                                             color=None,
                                             add_data=False)
        self.distractor = record
        self.distractor_id = data["id"]

        ## scale the distractor
        self.distractor_scale = self.rescale_record_to_size(record, self.distractor_scale_range, randomize=True)

        ## choose its position
        self._choose_distractor_position()

        ## choose its pose/rotation
        self._choose_distractor_rotation()

        if self.PRINT:
            print("DISTRACTOR POS", self.distractor_position)

        add_distractor_cmds = self.add_primitive(
            record=self.distractor,
            position=self.distractor_position,
            rotation=self.distractor_rotation,
            scale=self.distractor_scale,
            material=self.distractor_material,
            color=data["color"],
            mass=2.0,
            static_friction=1.0,
            dynamic_friction=1.0,
            bounciness=0.0,
            scale_mass=False,
            o_id=self.distractor_id,
            add_data=True,
            make_kinematic=False,
            apply_texture=(True if self.distractor_material else False)
        )
        commands.extend(add_distractor_cmds)

        return commands

    def get_trial_initialization_commands(self) -> List[dict]:
        commands = []

        ## randomization across trials
        if not(self.randomize):
            self.trial_seed = (self.MAX_TRIALS * self.seed) + self._trial_num
            random.seed(self.trial_seed)
        else:
            self.trial_seed = -1 # not used

        print("CONTROLLER SEED: %d" % self.seed)
        print("TRIAL SEED: %d" % self.trial_seed)


        ## place "zone" (i.e. a mat on the floor)
        commands.extend(self._place_target_zone())

        ## place container
        commands.extend(self._place_container())

        ## teleport the avatar
        commands.extend(self._place_camera())

        ## place target (depends on camera position for occlude)
        commands.extend(self._place_target_object())

        ## place distractor (depends on camera position)
        commands.extend(self._place_distractor())

        return commands

    def get_per_frame_commands(self, resp: List[bytes], frame: int) -> List[dict]:

        return []

if __name__ == '__main__':

    import platform, os
    args = get_relational_args("relational")
    if platform.system() == 'Linux':
        if args.gpu is not None:
            os.environ["DISPLAY"] = ":0." + str(args.gpu)
        else:
            os.environ["DISPLAY"] = ":0"

        launch_build = True
    else:
        launch_build = True

    print(args.relation)

    RC = RelationArrangement(
        ## relation
        relation=args.relation,

        ## objects
        container=args.container,
        target=args.target,
        distractor=args.distractor,

        ## positions
        container_position_range=args.cposition,
        target_position_range=args.tposition,
        target_rotation_range=args.trotation,
        target_angle_range=args.tangle,
        target_position_jitter=args.tjitter,
        target_always_horizontal=args.thorizontal,

        ## scales
        zone_scale_range=args.zscale,
        container_scale_range=args.cscale,
        target_scale_range=args.tscale,
        distractor_scale_range=args.dscale,
        max_target_scale_ratio=args.max_target_scale_ratio,

        ## camera
        camera_radius=args.camera_distance,
        camera_min_angle=args.camera_min_angle,
        camera_max_angle=args.camera_max_angle,
        camera_min_height=args.camera_min_height,
        camera_max_height=args.camera_max_height,
        camera_left_right_reflections=args.camera_left_right_reflections,

        ## common
        launch_build=launch_build,
        port=args.port,
        room=args.room,
        randomize=args.random,
        seed=args.seed,
        flex_only=False
    )

    if bool(args.run):
        RC.run(num=args.num,
                 output_dir=args.dir,
               temp_path=args.temp,
                 width=args.width,
                 height=args.height,
                 framerate=args.framerate,
                 save_passes=args.save_passes.split(','),
                 save_movies=args.save_movies,
                 save_labels=args.save_labels,
                 save_meshes=args.save_meshes,
                 write_passes=args.write_passes,
                 args_dict=vars(args)
        )
    else:
        RC.communicate({"$type": "terminate"})
