"""
Arbor class and member functions



"""

#-----------------------------------------------------------------------------
# Copyright (c) 2016-2017, Britton Smith <brittonsmith@gmail.com>
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING.txt, distributed with this software.
#-----------------------------------------------------------------------------

from collections import \
    defaultdict
import functools
import glob
import json
import numpy as np
import os

from yt.extern.six import \
    add_metaclass, \
    string_types
from yt.frontends.ytdata.utilities import \
    save_as_dataset
from yt.funcs import \
    ensure_dir, \
    get_pbar
from yt.units.dimensions import \
    length
from yt.units.unit_registry import \
    UnitRegistry
from yt.units.yt_array import \
    YTArray, \
    YTQuantity
from yt.utilities.cosmology import \
    Cosmology

from ytree.arbor.fields import \
    FakeFieldContainer, \
    FieldContainer, \
    FieldInfoContainer
from ytree.arbor.io import \
    FallbackRootFieldIO, \
    TreeFieldIO
from ytree.arbor.tree_node import \
    TreeNode
from ytree.arbor.tree_node_selector import \
    tree_node_selector_registry
from ytree.utilities.exceptions import \
    ArborFieldAlreadyExists, \
    ArborFieldDependencyNotFound
from ytree.utilities.logger import \
    fake_pbar, \
    ytreeLogger as mylog

arbor_registry = {}

class RegisteredArbor(type):
    """
    Add to the registry of known Arbor classes to cycle
    through in the load function.
    """
    def __init__(cls, name, b, d):
        type.__init__(cls, name, b, d)
        arbor_type = name[:name.rfind("Arbor")]
        if arbor_type:
            arbor_registry[arbor_type] = cls

@add_metaclass(RegisteredArbor)
class Arbor(object):
    """
    Base class for all Arbor classes.

    Loads a merger-tree output file or a series of halo catalogs
    and create trees, stored in an array in :func:`~ytree.arbor.Arbor.trees`.
    Arbors can be saved in a universal format with
    :func:`~ytree.arbor.Arbor.save_arbor`.  Also, provide some convenience
    functions for creating YTArrays and YTQuantities and a cosmology
    calculator.
    """

    _field_info_class = FieldInfoContainer
    _root_field_io_class = FallbackRootFieldIO
    _tree_field_io_class = TreeFieldIO

    def __init__(self, filename):
        """
        Initialize an Arbor given an input file.
        """

        self.filename = filename
        self.basename = os.path.basename(filename)
        self.unit_registry = UnitRegistry()
        self._parse_parameter_file()
        self._set_units()
        self._root_field_data = FieldContainer(self)
        self._setup_fields()
        self._set_default_selector()
        self._node_io = self._tree_field_io_class(self)
        self._root_io = self._root_field_io_class(self)

    def _parse_parameter_file(self):
        """
        Read relevant parameters from parameter file or file header
        and detect fields.
        """
        raise NotImplementedError

    def _plant_trees(self):
        """
        Create the list of root tree nodes.
        """
        raise NotImplementedError

    def is_setup(self, tree_node):
        return tree_node.root != -1 or \
          tree_node._uids is not None

    def _setup_tree(self, tree_node, **kwargs):
        """
        Create arrays of uids and descids and attach them to the
        root node.
        """
        # skip if this is not a root or if already setup
        if self.is_setup(tree_node):
            return

        idtype      = np.int64
        grow_fields = ["id", "desc_id"]
        dtypes      = {"id": idtype, "desc_id": idtype}
        field_data  = self._node_io._read_fields(tree_node, grow_fields,
                                                 dtypes=dtypes, **kwargs)
        uids    = field_data["id"]
        descids = field_data["desc_id"]
        tree_node._uids      = uids
        tree_node._descids   = descids
        tree_node._tree_size = uids.size

    def is_grown(self, tree_node):
        return hasattr(tree_node, "treeid")

    def _grow_tree(self, tree_node):
        """
        Create an array of TreeNodes hanging off the root node
        and assemble the tree structure.
        """
        # skip this if not a root or if already grown
        if self.is_grown(tree_node):
            return

        self._setup_tree(tree_node)
        nhalos   = tree_node.uids.size
        nodes    = np.empty(nhalos, dtype=np.object)
        nodes[0] = tree_node
        for i in range(1, nhalos):
            nodes[i] = TreeNode(tree_node.uids[i], arbor=self)
        tree_node._nodes = nodes

        uidmap   = {}
        for i, node in enumerate(nodes):
            node.treeid = i
            node.root   = tree_node
            descid      = tree_node.descids[i]
            uidmap[tree_node.uids[i]] = i
            if descid != -1:
                desc = nodes[uidmap[tree_node.descids[i]]]
                desc.add_ancestor(node)
                node.descendent = desc

    def _create_nodes(self, tree_node):
        self._grow_tree(tree_node)

    def _node_io_loop(self, func, *args, **kwargs):
        root_nodes = kwargs.pop("root_nodes", None)
        if root_nodes is None:
            root_nodes = self.trees
        pbar = kwargs.pop("pbar", None)
        if pbar is None:
            mypbar = fake_pbar
        else:
            mypbar = get_pbar

        pbar = mypbar(pbar, len(root_nodes))
        for node in root_nodes:
            func(node, *args, **kwargs)
            pbar.update(1)
        pbar.finish()

    _trees = None
    @property
    def trees(self):
        if self._trees is None:
            self._plant_trees()
        return self._trees

    def __repr__(self):
        return self.basename

    def __getitem__(self, key):
        """
        If given a string, return an array of field values for the
        roots of all trees.
        If given an integer, return a tree from the list of trees.

        """
        if isinstance(key, string_types):
            if key in ("tree", "prog"):
                raise SyntaxError("Argument must be a field or integer.")
            self._root_io.get_fields(self, fields=[key])
            return self._root_field_data[key]
        return self.trees[key]

    def __iter__(self):
        """
        Iterate over all items in the tree list.
        """
        for t in self.trees:
            yield t

    def __len__(self):
        """
        Return length of tree list.
        """
        return self.trees.size

    _field_info = None
    @property
    def field_info(self):
        if self._field_info is None and \
          self._field_info_class is not None:
            self._field_info = self._field_info_class(self)
        return self._field_info

    @property
    def size(self):
        """
        Return length of tree list.
        """
        return self.trees.size

    _unit_registry = None
    @property
    def unit_registry(self):
        return self._unit_registry

    @unit_registry.setter
    def unit_registry(self, value):
        self._unit_registry = value
        self._arr = None
        self._quan = None

    _hubble_constant = None
    @property
    def hubble_constant(self):
        return self._hubble_constant

    @hubble_constant.setter
    def hubble_constant(self, value):
        self._hubble_constant = value
        # reset the unit registry lut while preserving other changes
        self.unit_registry = UnitRegistry.from_json(
            self.unit_registry.to_json())
        self.unit_registry.modify("h", self.hubble_constant)

    _box_size = None
    @property
    def box_size(self):
        return self._box_size

    @box_size.setter
    def box_size(self, value):
        self._box_size = value
        # set unitary as soon as we know the box size
        self.unit_registry.add(
            "unitary", float(self.box_size.in_base()), length)

    def _setup_fields(self):
        self.derived_field_list = []
        self.analysis_field_list = []
        self.field_info.setup_aliases()
        self.field_info.setup_derived_fields()

    def _set_units(self):
        """
        Set "cm" units for explicitly comoving.
        Note, we are using comoving units all the time since
        we are dealing with data at multiple redshifts.
        """
        for my_unit in ["m", "pc", "AU", "au"]:
            new_unit = "%scm" % my_unit
            self._unit_registry.add(
                new_unit, self._unit_registry.lut[my_unit][0],
                length, self._unit_registry.lut[my_unit][3])

        self.cosmology = Cosmology(
            hubble_constant=self.hubble_constant,
            omega_matter=self.omega_matter,
            omega_lambda=self.omega_lambda,
            unit_registry=self.unit_registry)

    def set_selector(self, selector, *args, **kwargs):
        r"""
        Sets the tree node selector to be used.

        This sets the manner in which halo progenitors are
        chosen from a list of ancestors.  The most obvious example
        is to select the most massive ancestor.

        Parameters
        ----------
        selector : string
            Name of the selector to be used.

        Any additional arguments and keywords to be provided to
        the selector function should follow.

        Examples
        --------

        >>> import ytree
        >>> a = ytree.load("rockstar_halos/trees/tree_0_0_0.dat")
        >>> a.set_selector("max_field_value", "mass")

        """
        self.selector = tree_node_selector_registry.find(
            selector, *args, **kwargs)

    _arr = None
    @property
    def arr(self):
        """
        Create a YTArray using the Arbor's unit registry.
        """
        if self._arr is not None:
            return self._arr
        self._arr = functools.partial(YTArray,
                                      registry=self.unit_registry)
        return self._arr

    _quan = None
    @property
    def quan(self):
        """
        Create a YTQuantity using the Arbor's unit registry.
        """
        if self._quan is not None:
            return self._quan
        self._quan = functools.partial(YTQuantity,
                                       registry=self.unit_registry)
        return self._quan

    def _set_default_selector(self):
        """
        Set the default tree node selector as maximum mass.
        """
        self.set_selector("max_field_value", "mass")

    def select_halos(self, criteria, trees=None, select_from="tree",
                     fields=None):
        """
        Select halos from the arbor based on a set of criteria given as a string.
        """

        if select_from not in ["tree", "prog"]:
            raise SyntaxError(
                "Keyword \"select_from\" must be either \"tree\" or \"prog\".")

        if trees is None:
            trees = self.trees

        if fields is None:
            fields = []

        self._node_io_loop(self._setup_tree, root_nodes=trees,
                           pbar="Setting up trees")
        if fields:
            self._node_io_loop(
                self._node_io.get_fields,
                pbar="Getting fields",
                root_nodes=trees, fields=fields, root_only=False)


        halos = []
        pbar = get_pbar("Selecting halos", self.trees.size)
        for tree in trees:
            my_filter = eval(criteria)
            halos.extend(tree[select_from][my_filter])
            pbar.update(1)
        pbar.finish()
        return np.array(halos)

    def add_analysis_field(self, name, units):
        r"""
        Add an empty field to be filled by analysis operations.

        Parameters
        ----------
        name : string
            Field name.
        units : string
            Field units.

        Examples
        --------

        >>> import ytree
        >>> a = ytree.load("tree_0_0_0.dat")
        >>> a.add_analysis_field("robots", "Msun * kpc")
        >>> # Set field for some halo.
        >>> a[0]["tree"][7]["robots"] = 1979.816
        """

        if name in self.field_info:
            raise ArborFieldAlreadyExists(name, arbor=self)

        self.analysis_field_list.append(name)
        self.field_info[name] = {"type": "analysis",
                                 "units": units}

    def add_alias_field(self, alias, field, units=None,
                        force_add=True):
        r"""
        Add a field as an alias to another field.

        Parameters
        ----------
        alias : string
            Alias name.
        field : string
            The field to be aliased.
        units : optional, string
            Units in which the field will be returned.
        force_add : optional, bool
            If True, add field even if it already exists and warn the
            user and raise an exception if dependencies do not exist.
            If False, silently do nothing in both instances.
            Default: True.

        Examples
        --------

        >>> import ytree
        >>> a = ytree.load("tree_0_0_0.dat")
        >>> # "Mvir" exists on disk
        >>> a.add_alias_field("mass", "Mvir", units="Msun")
        >>> print (a["mass"])

        """

        if alias in self.field_info:
            if force_add:
                ftype = self.field_info[alias].get("type", "on-disk")
                if ftype in ["alias", "derived"]:
                    fl = self.derived_field_list
                else:
                    fl = self.field_list
                mylog.warn(
                    ("Overriding field \"%s\" that already " +
                     "exists as %s field.") % (alias, ftype))
                fl.pop(fl.index(alias))
            else:
                return

        if field not in self.field_info:
            if force_add:
                raise ArborFieldDependencyNotFound(
                    field, alias, arbor=self)
            else:
                return

        if units is None:
            units = self.field_info[field].get("units")
        self.derived_field_list.append(alias)
        self.field_info[alias] = \
          {"type": "alias", "units": units,
           "dependencies": [field]}
        if "aliases" not in self.field_info[field]:
            self.field_info[field]["aliases"] = []
            self.field_info[field]["aliases"].append(alias)

    def add_derived_field(self, name, function,
                          units=None, description=None,
                          force_add=True):
        r"""
        Add a field that is a function of other fields.

        Parameters
        ----------
        name : string
            Field name.
        function : callable
            The function to be called to generate the field.
            This function should take two arguments, the
            arbor and the data structure containing the
            dependent fields.  See below for an example.
        units : optional, string
            The units in which the field will be returned.
        description : optional, string
            A short description of the field.
        force_add : optional, bool
            If True, add field even if it already exists and warn the
            user and raise an exception if dependencies do not exist.
            If False, silently do nothing in both instances.
            Default: True.

        Examples
        --------

        >>> import ytree
        >>> a = ytree.load("tree_0_0_0.dat")
        >>> def _redshift(arbor, data):
        ...     return 1. / data["scale"] - 1
        ...
        >>> a.add_derived_field("redshift", _redshift)
        >>> print (a["redshift"])

        """

        if name in self.field_info:
            if force_add:
                ftype = self.field_info[name].get("type", "on-disk")
                if ftype in ["alias", "derived"]:
                    fl = self.derived_field_list
                else:
                    fl = self.field_list
                mylog.warn(
                    ("Overriding field \"%s\" that already " +
                     "exists as %s field.") % (name, ftype))
                fl.pop(fl.index(name))
            else:
                return

        if units is None:
            units = ""
        fc = FakeFieldContainer(self, name=name)
        try:
            rv = function(fc)
        except ArborFieldDependencyNotFound as e:
            if force_add:
                raise e
            else:
                return
        rv.convert_to_units(units)
        self.derived_field_list.append(name)
        self.field_info[name] = \
          {"type": "derived", "function": function,
           "units": units, "description": description,
           "dependencies": list(fc.keys())}

    @classmethod
    def _is_valid(cls, *args, **kwargs):
        """
        Check if input file works with a specific Arbor class.
        This is used with :func:`~ytree.arbor.load` function.
        """
        return False

    def save_arbor(self, filename="arbor", fields=None, trees=None,
                   max_file_size=524288):
        r"""
        Save the arbor to a file.

        The saved arbor can be re-loaded as an arbor.

        Parameters
        ----------
        filename : optional, string
            Output file keyword.  Main header file will be named
            <filename>/<filename>.h5.
            Default: "arbor".
        fields : optional, list of strings
            The fields to be saved.  If not given, all
            fields will be saved.

        Returns
        -------
        filename : string
            The filename of the saved arbor.

        Examples
        --------

        >>> import ytree
        >>> a = ytree.load("rockstar_halos/trees/tree_0_0_0.dat")
        >>> fn = a.save_arbor()
        >>> # reload it
        >>> a2 = ytree.load(fn)

        """

        if trees is None:
            trees = self.trees
        else:
            raise NotImplementedError

        if fields in [None, "all"]:
            # If a field has an alias, get that instead.
            fields = []
            for field in self.field_list + self.analysis_field_list:
                fields.extend(
                    self.field_info[field].get("aliases", [field]))

        ds = {}
        for attr in ["hubble_constant",
                     "omega_matter",
                     "omega_lambda"]:
            if hasattr(self, attr):
                ds[attr] = getattr(self, attr)
        extra_attrs = {"box_size": self.box_size,
                       "arbor_type": "YTreeArbor",
                       "unit_registry_json": self.unit_registry.to_json()}

        self._node_io_loop(self._setup_tree,
                           pbar="Setting up trees")
        self._root_io.get_fields(self, fields=fields)

        # determine file layout
        nn = 0
        nt = 0
        nnodes = []
        ntrees = []
        tree_size = np.array([tree.tree_size for tree in self.trees])
        for ts in tree_size:
            nn += ts
            nt += 1
            if nn > max_file_size:
                nnodes.append(nn-ts)
                ntrees.append(nt-1)
                nn = ts
                nt = 1
        if nn > 0:
            nnodes.append(nn)
            ntrees.append(nt)
        nfiles = len(nnodes)
        nnodes = np.array(nnodes)
        ntrees = np.array(ntrees)
        tree_end_index   = ntrees.cumsum()
        tree_start_index = tree_end_index - ntrees

        # write header file
        fieldnames = [field.replace("/", "_") for field in fields]
        myfi = {}
        rdata = {}
        rtypes = {}
        for field, fieldname in zip(fields, fieldnames):
            fi = self.field_info[field]
            myfi[fieldname] = \
              dict((key, fi[key])
                   for key in ["units", "description"]
                   if key in fi)
            rdata[fieldname] = self._root_field_data[field]
            rtypes[fieldname] = "data"
        extra_attrs["field_info"] = json.dumps(myfi)
        extra_attrs["total_files"] = nfiles
        extra_attrs["total_trees"] = self.trees.size
        extra_attrs["total_nodes"] = tree_size.sum()
        hdata = {"tree_start_index": tree_start_index,
                 "tree_end_index"  : tree_end_index,
                 "tree_size"       : ntrees}
        hdata.update(rdata)
        htypes = dict((f, "index") for f in hdata)
        htypes.update(rtypes)

        ensure_dir(filename)
        header_filename = os.path.join(filename, "%s.h5" % filename)
        save_as_dataset(ds, header_filename, hdata,
                        field_types=htypes,
                        extra_attrs=extra_attrs)

        ftypes = dict((f, "data") for f in fieldnames)
        for i in range(nfiles):
            my_nodes = self.trees[tree_start_index[i]:tree_end_index[i]]
            self._node_io_loop(
                self._node_io.get_fields,
                pbar="Getting fields [%d/%d]" % (i+1, nfiles),
                root_nodes=my_nodes, fields=fields, root_only=False)
            fdata = dict((field, np.empty(nnodes[i])) for field in fieldnames)
            my_tree_size  = tree_size[tree_start_index[i]:tree_end_index[i]]
            my_tree_end   = my_tree_size.cumsum()
            my_tree_start = my_tree_end - my_tree_size
            pbar = get_pbar("Creating field arrays [%d/%d]" %
                            (i+1, nfiles), len(fields)*nnodes[i])
            c = 0
            for field, fieldname in zip(fields, fieldnames):
                for di, node in enumerate(my_nodes):
                    fdata[fieldname][my_tree_start[di]:my_tree_end[di]] = \
                      node._tree_field_data[field]
                    c += my_tree_size[di]
                    pbar.update(c)
            pbar.finish()
            fdata["tree_start_index"] = my_tree_start
            fdata["tree_end_index"]   = my_tree_end
            fdata["tree_size"]        = my_tree_size
            for ft in ["tree_start_index",
                      "tree_end_index",
                      "tree_size"]:
                ftypes[ft] = "index"
            my_filename = os.path.join(
                filename, "%s_%04d.h5" % (filename, i))
            save_as_dataset({}, my_filename, fdata,
                            field_types=ftypes)

        return header_filename

class CatalogArbor(Arbor):
    """
    Base class for Arbors created from a series of halo catalog
    files where the descendent ID for each halo has been
    pre-determined.
    """

    _prefix = None
    _data_file_class = None

    def __init__(self, filename):
        self.filename = filename
        self._get_data_files()
        super(CatalogArbor, self).__init__(filename)

    def _get_data_files(self):
        raise NotImplementedError

    def _plant_trees(self):
        fields, _ = \
          self.field_info.resolve_field_dependencies(["halo_id", "desc_id"])
        halo_id_f, desc_id_f = fields
        dtypes = dict((field, np.int64) for field in fields)
        uid = 0
        trees = []
        nfiles = len(self.data_files)
        for i, dfl in enumerate(self.data_files):
            if not isinstance(dfl, list):
                dfl = [dfl]

            batches = []
            bsize = []
            hids = []
            ancs = defaultdict(list)
            for data_file in dfl:
                data = data_file._read_fields(fields, dtypes=dtypes)
                nhalos = len(data[halo_id_f])
                batch = np.empty(nhalos, dtype=object)

                for it in range(nhalos):
                    descid = data[desc_id_f][it]
                    root = i == 0 or descid == -1
                    tree_node = TreeNode(uid, arbor=self, root=root)
                    tree_node._fi = it
                    tree_node.data_file = data_file
                    batch[it] = tree_node
                    if root:
                        trees.append(tree_node)
                    else:
                        ancs[descid].append(tree_node)
                    uid += 1
                data_file.trees = batch
                batches.append(batch)
                bsize.append(batch.size)
                hids.append(data[halo_id_f])

            if i > 0:
                for descid, ancestors in ancs.items():
                    # this will not be fast
                    descendent = descs[descid == lastids][0]
                    descendent._ancestors = ancestors
                    for ancestor in ancestors:
                        ancestor.descendent = descendent

            if i < nfiles - 1:
                descs = np.empty(sum(bsize), dtype=object)
                lastids = np.empty(descs.size, dtype=np.int64)
                ib = 0
                for batch, hid, bs in zip(batches, hids, bsize):
                    descs[ib:ib+bs] = batch
                    lastids[ib:ib+bs] = hid
                    ib += bs

        self._trees = np.array(trees)

    def _setup_tree(self, tree_node):
        if self.is_setup(tree_node):
            return

        nodes   = []
        uids    = []
        descids = [-1]
        for i, node in enumerate(tree_node.twalk()):
            node.treeid = i
            node.root   = tree_node
            nodes.append(node)
            uids.append(node.uid)
            if i > 0:
                descids.append(node.descendent.uid)
        tree_node._nodes     = np.array(nodes)
        tree_node._uids      = np.array(uids)
        tree_node._descids   = np.array(descids)
        tree_node._tree_size = tree_node._uids.size

    def _create_nodes(self, tree_node):
        self._setup_tree(tree_node)

    def _grow_tree(self, tree_node):
        pass

    def is_grown(self, tree_node):
        return True

def load(filename, method=None):
    """
    Load an Arbor, determine the type automatically.

    Parameters
    ----------
    filename : string
        Input filename.
    method : optional, string
        The type of Arbor to be loaded.  Existing types are:
        Arbor, ConsistentTrees, Rockstar, TreeFar.  If not
        given, the type will be determined based on characteristics
        of the input file.

    Returns
    -------
    Arbor

    Examples
    --------

    >>> import ytree
    >>> # saved Arbor
    >>> a = ytree.load("arbor.h5")
    >>> # consistent-trees output
    >>> a = ytree.load("rockstar_halos/trees/tree_0_0_0.dat")
    >>> # Rockstar catalogs
    >>> a = ytree.load("rockstar_halos/out_0.list")
    >>> # TreeFarm catalogs
    >>> a = ytree.load("my_halos/fof_subhalo_tab_025.0.hdf5.0.h5")

    """
    if not os.path.exists(filename):
        raise IOError("file does not exist: %s." % filename)
    if method is None:
        candidates = []
        for candidate, c in arbor_registry.items():
            if c._is_valid(filename):
                candidates.append(candidate)
        if len(candidates) == 0:
            raise IOError("Could not determine arbor type for %s." % filename)
        elif len(candidates) > 1:
            errmsg = "Could not distinguish between these arbor types:\n"
            for candidate in candidates:
                errmsg += "Possible: %s.\n" % candidate
            errmsg += "Provide one of these types using the \'method\' keyword."
            raise IOError(errmsg)
        else:
            method = candidates[0]
    else:
        if method not in arbor_registry:
            raise IOError("Invalid method: %s.  Available: %s." %
                          (method, arbor_registry.keys()))
    return arbor_registry[method](filename)
