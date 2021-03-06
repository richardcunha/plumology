"""hdf - HDF5 wrappers and utilities"""

from typing import (Any, Sequence, List, Mapping,
                    Callable, Union, Optional)

import h5py
import numpy as np
import pandas as pd

from .rw import read_plumed_fields

__all__ = ['plumed_to_h5', 'plumed_to_hdf', 'hdf_to_dataframe']


def plumed_to_hdf(
        files: Union[List[str], str],
        hdf_file: str,
        keys: Union[List[str], str],
        field_map: Optional[Mapping[str, str]]=None,
        columnwise: bool=False,
        overwrite: bool=False,
) -> None:
    """
    Read PLUMED files and dump to HDF5.

    Parameters
    ----------
    files : Files generated by PLUMED.
    hdf_file : Path to HDF file.
    keys : Keys to be used for top-level HDF groups.
    field_map : Mapping to replace potentially bad names for fields.
    columnwise : Operate on columns instead of whole files,
        this is easier on memory, but far slower.
    overwrite : Overwrite an existing file, or just append.

    """

    # Check input
    if not isinstance(files, list):
        files = [files]
    if not isinstance(keys, list):
        keys = [keys]
    if len(keys) != len(files):
        raise ValueError(
            'You must supply the same number of callables as files!'
        )
    fmode = 'w' if overwrite else 'a'

    with h5py.File(hdf_file, fmode) as store:

        for key, file in zip(keys, files):

            fields = read_plumed_fields(file)

            # Replace fields with better names
            if field_map is not None:
                fields = [
                    field_map[f] if f in field_map else f for f in fields
                ]

            # Prepare hdf structure
            grp = store.create_group(key)

            # This is lighter on memory, but slower
            if columnwise:
                for field in fields:

                    column = pd.read_csv(
                        file,
                        header=None,
                        comment='#',
                        names=fields,
                        sep=r'\s+',
                        dtype=np.float64,
                        usecols=[field]
                    ).values.flatten()

                    grp.create_dataset(field, data=column)

            # Memory heavy, but fast
            else:

                dataset = pd.read_csv(
                    file,
                    header=None,
                    comment='#',
                    names=fields,
                    sep=r'\s+',
                    dtype=np.float64,
                    usecols=fields
                )

                for field in fields:
                    grp.create_dataset(
                        field, data=dataset[field].values.flatten()
                    )


def hdf_to_dataframe(
        hdf_file: str,
        reduce: Optional[int]=None,
        aggregator: Optional[Callable[[np.ndarray], float]]=None,
        weight: bool=True,
        reshape: bool=True,
        grouper: str='ff',
        weight_name: str='ww'
) -> pd.DataFrame:
    """
    Read HDF file to dataframe and reduce number of datapoints.

    Parameters
    ----------
    hdf_file : Path to HDF file.
    reduce : Skip this number of lines when reading the file.
    aggregator : Function taking a column as input and returning a float.
    weight : Whether to reweight the data using a weight column.
    reshape : If true, use wide_to_long to add residue index.
    grouper : Name of the index determining the simulation.
    weight_name : Name of the weight column.

    Returns
    -------
    df : Multiindexed dataframe with the type of datapoint as columns
        and time, residue number and HDF top-level key as indices.

    """
    if reduce is not None and aggregator is not None:
        raise TypeError(
            'You can not specify both a reduction and an aggregator!'
        )

    frames = []
    with h5py.File(hdf_file, 'r') as store:

        # Iterate through HDF groups
        for key in store.keys():

            # Reducing density
            if reduce is not None:
                raw = pd.DataFrame({
                    k: v[::reduce] for k, v in store[key].items()
                })

                # Renormalize weights
                raw[weight_name] /= raw[weight_name].sum()
                raw[grouper] = key

            # Aggregate over time
            else:
                raw = {}

                # Iterate through columns
                for k, v in store[key].items():

                    # Time and weights will be lost anyway
                    if k in ['time', weight_name]:
                        continue
                    elif weight:
                        raw[k] = aggregator(
                            v[()] * store[key][weight_name][()]
                        )
                    else:
                        raw[k] = aggregator(v[()])

                raw[grouper] = key
                raw = pd.DataFrame(raw, index=[key])

            frames.append(raw)

    raw_frame = pd.concat(frames)

    if reshape:
        # Unique columns for wide_to_long
        cols = list({v.rstrip('0123456789') for v in raw_frame.columns
                     if v not in ['time', grouper, weight_name]})

        # Move residue number to index
        index = 'time' if reduce is not None else grouper
        df = pd.wide_to_long(raw_frame, cols, i=index, j='res_nr')

    else:
        df = raw_frame

    # Remove superfluous columns
    if reduce is not None:
        df = df.set_index([grouper], append=True)
        if 'time' in df:
            del df['time']
    else:
        if grouper in df:
            del df[grouper]

    return df.sort_index(axis=1).sort_index()


def plumed_to_h5(
        files: Union[str, List[str]],
        hdf_file: str,
        func: Union[List[Callable[[pd.DataFrame], pd.DataFrame]],
                    Callable[[pd.DataFrame], pd.DataFrame], None]=None,
        chunksize: int=10000,
        verbose: bool=True,
        kwargs: Union[Mapping[str, Any],
                      Sequence[Mapping[str, Any]], None]=None
) -> None:
    """
    Read PLUMED files and dump to pytables HDF5.

    Parameters
    ----------
    files : File(s) generated by PLUMED.
    hdf_file : Path to HDF file.
    func : Function(s) operating on dataframe chunks.
    chunksize : Size of the chunks to be read by pd.read_csv()
    verbose : Be more verbose.
    kwargs : Arguments to be passed to func.

    """

    # Check input
    if isinstance(files, list):
        files = [files]
    if isinstance(func, list):
        func = [func]
    if isinstance(kwargs, list):
        kwargs = [kwargs]
    if func[0] is not None and len(func) != len(files):
        raise ValueError(
            'You must supply the same number of callables as files!'
        )

    for j, file in enumerate(files):

        # Read column names
        fields = read_plumed_fields(file)

        # Prepare iterator over chunks
        chunker = pd.read_csv(
            file,
            header=None,
            comment='#',
            names=fields,
            sep=r'\s+',
            chunksize=chunksize,
            dtype=np.float64
        )

        # Iterate over chunks
        for i, chunk in enumerate(chunker):

            # We make sure each file has a unique entry
            if func is not None:
                chunk = func[j](chunk, **kwargs[j])
            else:
                chunk['file'] = j

            chunk = chunk.sort_index(axis=1)
            chunk.to_hdf(hdf_file, 'df', append=True)

            if verbose:
                print('Writing chunk {0}'.format(i))
