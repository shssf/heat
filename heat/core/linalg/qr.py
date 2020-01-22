import collections
import torch

from .. import dndarray
from .. import factories
from .. import tiling

__all__ = ["qr"]


def qr(a, tiles_per_proc=1, calc_q=True, overwrite_a=False):
    """
    Calculates the QR decomposition of a 2D DNDarray.
    Factor the matrix `a` as *qr*, where `q` is orthonormal and `r` is upper-triangular.

    Parameters
    ----------
    a : DNDarray
        DNDarray which will be decomposed
    tiles_per_proc : int, singlt element torch.Tensor
        optional, default: 1
        number of tiles per process to operate on
    calc_q : bool
        optional, default: True
        whether or not to calculate Q
        if True, function returns (Q, R)
        if False, function returns (None, R)
    overwrite_a : bool
        optional, default: False
        if True, function overwrites the DNDarray a, with R
        if False, a new array will be created for R

    Returns
    -------
    namedtuple of Q and R
        if calc_q == True, function returns QR(Q=Q, R=R)
        if calc_q == False, function returns QR(Q=None, R=R)

    Notes
    -----
    This function is built on top of PyTorch's QR function. torch.qr() using LAPACK on the backend.
    Basic information about QR factorization/decomposition can be found at
    https://en.wikipedia.org/wiki/QR_factorization

    The algorithms are based on the CAQR and TSQRalgorithms. For more information see references.

    References
    ----------
    [0]  W. Zheng, F. Song, L. Lin, and Z. Chen, “Scaling Up Parallel Computation of Tiled QR
            Factorizations by a Distributed Scheduling Runtime System and Analytical Modeling,”
            Parallel Processing Letters, vol. 28, no. 01, p. 1850004, 2018.
    [1] Bilel Hadri, Hatem Ltaief, Emmanuel Agullo, Jack Dongarra. Tile QR Factorization with
            Parallel Panel Processing for Multicore Architectures. 24th IEEE International Parallel
            and DistributedProcessing Symposium (IPDPS 2010), Apr 2010, Atlanta, United States.
            inria-00548899
    [2] Gene H. Golub and Charles F. Van Loan. 1996. Matrix Computations (3rd Ed.).

    Examples
    --------
    >>> a = ht.random.randn(9, 6, split=0)
    >>> qr = ht.linalg.qr(a)
    >>> print(ht.allclose(a, ht.dot(qr.Q, qr.R)))
    [0/1] True
    [1/1] True
    >>> st = torch.randn(9, 6)
    >>> a = ht.array(st, split=1)
    >>> a_comp = ht.array(st, split=0)
    >>> q, r = ht.linalg.qr(a)
    >>> print(ht.allclose(a_comp, ht.dot(q, r)))
    [0/1] True
    [1/1] True
    """
    if not isinstance(a, dndarray.DNDarray):
        raise TypeError("'a' must be a DNDarray")
    if not isinstance(tiles_per_proc, (int, torch.Tensor)):
        raise TypeError(
            "tiles_per_proc must be an int or a torch.Tensor, "
            "currently {}".format(type(tiles_per_proc))
        )
    if not isinstance(calc_q, bool):
        raise TypeError("calc_q must be a bool, currently {}".format(type(calc_q)))
    if not isinstance(overwrite_a, bool):
        raise TypeError("overwrite_a must be a bool, currently {}".format(type(overwrite_a)))
    if isinstance(tiles_per_proc, torch.Tensor):
        raise ValueError(
            "tiles_per_proc must be a single element torch.Tenor or int, "
            "currently has {} entries".format(tiles_per_proc.numel())
        )
    if len(a.shape) != 2:
        raise ValueError("Array 'a' must be 2 dimensional")

    if a.split == 0:
        q, r = __qr_split0(
            a=a, tiles_per_proc=tiles_per_proc, calc_q=calc_q, overwrite_a=overwrite_a
        )
    elif a.split == 1:
        q, r = __qr_split1(
            a=a, tiles_per_proc=tiles_per_proc, calc_q=calc_q, overwrite_a=overwrite_a
        )
    elif a.split is None:
        q, r = a._DNDarray__array.qr(some=False)
        q = factories.array(q, device=a.device)
        r = factories.array(r, device=a.device)

    QR = collections.namedtuple("QR", "Q, R")
    ret = QR(q if calc_q else None, r)
    return ret


def __global_q_dict_set(q_dict_col, col, a_tiles, q_tiles, global_merge_dict=None):
    """
    The function takes the orginial Q tensors from the global QR calculation and sets them to
    the keys which corresponds with their tile coordinates in Q. this returns a separate dictionary,
    it does NOT set the values of Q

    Parameters
    ----------
    q_dict_col : Dict
        The dictionary of the Q values for a given column, should be given as q_dict[col]
    col : int, single element torch.Tensor
        current column for which Q is being calculated for
    a_tiles : tiling.SquareDiagTiles
        tiling object for 'a'
    q_tiles : tiling.SquareDiagTiles
        tiling object for Q0
    global_merge_dict : Dict, optional
        the ouput of the function will be in this dictionary
        Form of output: key index : torch.Tensor

    Returns
    -------
    None
    """
    # q is already created, the job of this function is to create the group the merging q's together
    # it takes the merge qs, splits them, then puts them into a new dictionary
    # steps
    proc_tile_start = torch.cumsum(
        torch.tensor(a_tiles.tile_rows_per_process, device=a_tiles.arr._DNDarray__array.device),
        dim=0,
    )
    diag_proc = torch.nonzero(proc_tile_start > col)[0].item()
    proc_tile_start = torch.cat(
        (torch.tensor([0], device=a_tiles.arr._DNDarray__array.device), proc_tile_start[:-1]), dim=0
    )

    # 1: create caqr dictionary
    # need to have empty lists for all tiles in q
    global_merge_dict = {} if global_merge_dict is None else global_merge_dict

    # intended to be used as [row][column] -> data
    # 2: loop over keys in the dictionary
    merge_list = list(q_dict_col.keys())
    merge_list.sort()
    # todo: possible improvement -> make the keys have the process they are on as well,
    #  then can async get them if they are not on the diagonal process
    for key in merge_list:
        # print(col, key)
        # this loops over all of the Qs for col and creates the dictionary for the pr Q merges
        p0 = key.find("p0")
        p1 = key.find("p1")
        end = key.find("e")
        r0 = int(key[p0 + 2 : p1])
        r1 = int(key[p1 + 2 : end])
        lp_q = q_dict_col[key][0]
        base_size = q_dict_col[key][1]
        # cut the q into 4 bits (end of base array)
        # todo: modify this so that it will get what is needed from the process,
        #  instead of gathering all the qs
        top_left = lp_q[: base_size[0], : base_size[0]]
        top_right = lp_q[: base_size[0], base_size[0] :]
        bottom_left = lp_q[base_size[0] :, : base_size[0]]
        bottom_right = lp_q[base_size[0] :, base_size[0] :]
        # need to adjust the keys to be the global row
        if diag_proc == r0:
            col0 = col
        else:
            col0 = proc_tile_start[r0].item()
        col1 = proc_tile_start[r1].item()
        # col0 and col1 are the columns numbers
        # r0 and r1 are the ranks

        # if there are no elements on that location than set it as the tile
        # 1. get keys of what already has data
        curr_keys = set(global_merge_dict.keys())
        # 2. determine which tiles need to be touched/created
        # these are the keys which are to be multiplied by the q in the current loop
        # for matrix of form: | J  K |
        #                     | L  M |
        mult_keys_00 = [(i, col0) for i in range(q_tiles.tile_columns)]  # (J)
        # (J) -> inds: (i, col0)(col0, col0) -> set at (i, col0)
        mult_keys_01 = [(i, col0) for i in range(q_tiles.tile_columns)]  # (K)
        # (K) -> inds: (i, col0)(col0, col1) -> set at (i, col1)
        mult_keys_10 = [(i, col1) for i in range(q_tiles.tile_columns)]  # (L)
        # (L) -> inds: (i, col1)(col1, col0) -> set at (i, col0)
        mult_keys_11 = [(i, col1) for i in range(q_tiles.tile_columns)]  # (M)
        # (M) -> inds: (i, col1)(col1, col1) -> set at (i, col1)

        # if there are no elements in the mult_keys then set the element to the same place
        s00 = set(mult_keys_00) & curr_keys
        s01 = set(mult_keys_01) & curr_keys
        s10 = set(mult_keys_10) & curr_keys
        s11 = set(mult_keys_11) & curr_keys
        hold_dict = global_merge_dict.copy()

        # (J)
        if not len(s00):
            global_merge_dict[col0, col0] = top_left
        else:  # -> do the mm for all of the mult keys
            for k in s00:
                global_merge_dict[k[0], col0] = hold_dict[k] @ top_left
        # (K)
        if not len(s01):
            # check that we are not overwriting here
            global_merge_dict[col0, col1] = top_right
        else:  # -> do the mm for all of the mult keys
            for k in s01:
                global_merge_dict[k[0], col1] = hold_dict[k] @ top_right
        # (L)
        if not len(s10):
            # check that we are not overwriting here
            global_merge_dict[col1, col0] = bottom_left
        else:  # -> do the mm for all of the mult keys
            for k in s10:
                global_merge_dict[k[0], col0] = hold_dict[k] @ bottom_left
        # (M)
        if not len(s11):
            # check that we are not overwriting here
            global_merge_dict[col1, col1] = bottom_right
        else:  # -> do the mm for all of the mult keys
            for k in s11:
                global_merge_dict[k[0], col1] = hold_dict[k] @ bottom_right
    return global_merge_dict


def __merge_tile_rows_qr(pr0, pr1, column, rank, a_tiles, diag_process, key, q_dict):
    """
    Merge two tile rows, take their QR, and apply it to the trailing process
    This will modify 'a' and set the value of the q_dict[column][key]
    with [Q, upper.shape, lower.shape].

    Parameters
    ----------
    pr0, pr1 : int, int
        Process ranks of the processes to be used
    column : int
        the current process of the QR calculation
    rank : int
        the rank of the process
    a_tiles : ht.tiles.SquareDiagTiles
        tiling object used for getting/setting the tiles required
    diag_process : int
        The rank of the process which has the tile along the diagonal for the given column

    Returns
    -------
    None, sets the value of q_dict[column][key] with [Q, upper.shape, lower.shape]
    """
    if rank not in [pr0, pr1]:
        return
    pr0 = pr0.item() if isinstance(pr0, torch.Tensor) else pr0
    pr1 = pr1.item() if isinstance(pr1, torch.Tensor) else pr1
    comm = a_tiles.arr.comm
    upper_row = sum(a_tiles.tile_rows_per_process[:pr0]) if pr0 != diag_process else column
    lower_row = sum(a_tiles.tile_rows_per_process[:pr1]) if pr1 != diag_process else column

    upper_inds = a_tiles.get_start_stop(key=(upper_row, column))
    lower_inds = a_tiles.get_start_stop(key=(lower_row, column))

    upper_size = (upper_inds[1] - upper_inds[0], upper_inds[3] - upper_inds[2])
    lower_size = (lower_inds[1] - lower_inds[0], lower_inds[3] - lower_inds[2])

    a_torch_device = a_tiles.arr._DNDarray__array.device

    # upper adjustments
    if upper_size[0] < upper_size[1] and a_tiles.tile_rows_per_process[pr0] > 1:
        # end of dim0 (upper_inds[1]) is equal to the size in dim1
        upper_inds = list(upper_inds)
        upper_inds[1] = upper_inds[0] + upper_size[1]
        upper_size = (upper_inds[1] - upper_inds[0], upper_inds[3] - upper_inds[2])
    if lower_size[0] < lower_size[1] and a_tiles.tile_rows_per_process[pr1] > 1:
        # end of dim0 (upper_inds[1]) is equal to the size in dim1
        lower_inds = list(lower_inds)
        lower_inds[1] = lower_inds[0] + lower_size[1]
        lower_size = (lower_inds[1] - lower_inds[0], lower_inds[3] - lower_inds[2])

    if rank == pr0:
        # need to use lloc on a_tiles.arr with the indices
        upper = a_tiles.arr.lloc[upper_inds[0] : upper_inds[1], upper_inds[2] : upper_inds[3]]

        comm.Send(upper.clone(), dest=pr1, tag=986)
        lower = torch.zeros(lower_size, dtype=a_tiles.arr.dtype.torch_type(), device=a_torch_device)
        comm.Recv(lower, source=pr1, tag=4363)
    if rank == pr1:
        lower = a_tiles.arr.lloc[lower_inds[0] : lower_inds[1], lower_inds[2] : lower_inds[3]]
        upper = torch.zeros(upper_size, dtype=a_tiles.arr.dtype.torch_type(), device=a_torch_device)
        comm.Recv(upper, source=pr0, tag=986)
        comm.Send(lower.clone(), dest=pr0, tag=4363)

    q_merge, r = torch.cat((upper, lower), dim=0).qr(some=False)
    upp = r[: upper.shape[0]]
    low = r[upper.shape[0] :]
    if rank == pr0:
        a_tiles.arr.lloc[upper_inds[0] : upper_inds[1], upper_inds[2] : upper_inds[3]] = upp
    if rank == pr1:
        a_tiles.arr.lloc[lower_inds[0] : lower_inds[1], lower_inds[2] : lower_inds[3]] = low

    if column < a_tiles.tile_columns - 1:
        upper_rest_size = (upper_size[0], a_tiles.arr.gshape[1] - upper_inds[3])
        lower_rest_size = (lower_size[0], a_tiles.arr.gshape[1] - lower_inds[3])

        if rank == pr0:
            upper_rest = a_tiles.arr.lloc[upper_inds[0] : upper_inds[1], upper_inds[3] :]
            lower_rest = torch.zeros(
                lower_rest_size, dtype=a_tiles.arr.dtype.torch_type(), device=a_torch_device
            )
            comm.Send(upper_rest.clone(), dest=pr1, tag=98654)
            comm.Recv(lower_rest, source=pr1, tag=436364)

        if rank == pr1:
            lower_rest = a_tiles.arr.lloc[lower_inds[0] : lower_inds[1], lower_inds[3] :]
            upper_rest = torch.zeros(
                upper_rest_size, dtype=a_tiles.arr.dtype.torch_type(), device=a_torch_device
            )
            comm.Recv(upper_rest, source=pr0, tag=98654)
            comm.Send(lower_rest.clone(), dest=pr0, tag=436364)

        cat_tensor = torch.cat((upper_rest, lower_rest), dim=0)
        new_rest = torch.matmul(q_merge.t(), cat_tensor)
        # the data for upper rest is a slice of the new_rest, need to slice only the 0th dim
        upp = new_rest[: upper_rest.shape[0]]
        low = new_rest[upper_rest.shape[0] :]
        if rank == pr0:
            a_tiles.arr.lloc[upper_inds[0] : upper_inds[1], upper_inds[3] :] = upp
        # set the lower rest
        if rank == pr1:
            a_tiles.arr.lloc[lower_inds[0] : lower_inds[1], lower_inds[3] :] = low

    q_dict[column][key] = [q_merge, upper.shape, lower.shape]


def __q_waits(q_dict_waits, q_dict, col):
    """
    In the q_dict_waits is all of the q values from the global merges

    Parameters
    ----------
    q_dict_waits : Dict
        Dictionary will all of the global merge Qs which were sent to the diagonal process
        these have the form:
        q_dict_waits[col][key] - the waits for one column, key is of the form
            'p0' + str(pr0) + 'p1' + str(pr1)
        q_dict_waits[col][key][0] -> Q
        q_dict_waits[col][key][1] -> comm.Wait
        q_dict_waits[col][key][2] -> upper shape
        q_dict_waits[col][key][3] -> lower shape
        q_dict_waits[col][key][4] -> loop number in the while loop
    q_dict : Dict
        Dictionary of the Q tensors used for the QR of the column 'col'
    col : int, 1 element torch.Tensor
        current column (current iteration of QR)

    Returns
    -------
    None
    Entries are added to q_dict[col] with a new key given by 'loop number' + p0 + # + pr #
    """
    if col not in q_dict_waits.keys():
        return
    for key in q_dict_waits[col].keys():
        new_key = q_dict_waits[col][key][3] + key + "e"
        q_dict_waits[col][key][0][1].wait()
        q_dict[col][new_key] = [
            q_dict_waits[col][key][0][0],
            q_dict_waits[col][key][1].wait(),
            q_dict_waits[col][key][2].wait(),
        ]
    del q_dict_waits[col]


def __q_calc_split0(a_tiles, q_tiles, col, q_dict, q_dict_waits, diag_process, active_procs):
    """
    Does the Q calculation for the QR of a split=0 DNDarray. This must be done in a separate loop
    than the R calculation for split=0, this is different from split=0 for algorithmic reasons. This
    function will wait for all of the Q values to arrive from other processes for a specific column
    Then it will merge them into the existing Q matrix using its tiles (q_tiles).

    Parameters
    ----------
    a_tiles : tiling.SquareDiagTiles
        tiling object for 'a'
    q_tiles : tiling.SquareDiagTiles
        tiling object for Q0
    col : int
        the current column of the the R calculation
    q_dict : Dict
        dictionary to save the calculated Q matrices to
    diag_process : int
        rank of the process which has the tile which lies along the diagonal
    active_procs : torch.Tensor
        tensor containing the processes which have not yet finished calculating Q
    Returns
    -------
    None
    """
    comm = a_tiles.arr.comm
    rank = comm.rank
    a_torch_device = a_tiles.arr.device.torch_device
    # wait for Q tensors sent during the R calculation ---------------------------------------------
    if col in q_dict_waits.keys():
        for key in q_dict_waits[col].keys():
            new_key = q_dict_waits[col][key][3] + key + "e"
            q_dict_waits[col][key][0][1].wait()
            q_dict[col][new_key] = [
                q_dict_waits[col][key][0][0],
                q_dict_waits[col][key][1].wait(),
                q_dict_waits[col][key][2].wait(),
            ]
        del q_dict_waits[col]
    # local Q calculation --------------------------------------------------------------------------
    if col in q_dict.keys():
        lcl_col_shape = a_tiles.local_get(key=(slice(None), col)).shape
        # get the start and stop of all local tiles
        #   -> get the rows_per_process[rank] and the row_indices
        row_ind = a_tiles.row_indices
        prev_rows_per_pr = sum(a_tiles.tile_rows_per_process[:rank])
        rows_per_pr = a_tiles.tile_rows_per_process[rank]
        if rows_per_pr == 1:
            # if there is only one tile on the process: return q_dict[col]['0']
            base_q = q_dict[col]["l0"][0].clone()
            del q_dict[col]["l0"]
        else:
            # 0. get the offset of the column start
            offset = (
                torch.tensor(
                    row_ind[col].item() - row_ind[prev_rows_per_pr].item(), device=a_torch_device
                )
                if row_ind[col].item() > row_ind[prev_rows_per_pr].item()
                else torch.tensor(0, device=a_torch_device)
            )
            # 1: create an eye matrix of the row's zero'th dim^2
            q_lcl = q_dict[col]["l0"]  # [0] -> q, [1] -> shape of a use in q calc (q is square)
            del q_dict[col]["l0"]
            base_q = torch.eye(
                lcl_col_shape[a_tiles.arr.split], dtype=q_lcl[0].dtype, device=a_torch_device
            )
            # 2: set the area of the eye as Q
            base_q[offset : offset + q_lcl[1][0], offset : offset + q_lcl[1][0]] = q_lcl[0]

        local_merge_q = {rank: [base_q, None]}
    else:
        local_merge_q = {}
    # -------------- send local Q to all -------------------------------------------------------
    q0_dtype = q_tiles.arr.dtype
    q0_torch_type = q0_dtype.torch_type()
    q0_torch_device = q_tiles.arr.device.torch_device
    for r in range(diag_process, active_procs[-1] + 1):
        if r != rank:
            hld = torch.zeros(
                [q_tiles.lshape_map[r][q_tiles.arr.split]] * 2,
                dtype=q0_dtype.torch_type(),
                device=a_torch_device,
            )
        else:
            hld = local_merge_q[r][0].clone()
        wait = comm.Ibcast(hld, root=r)
        local_merge_q[r] = [hld, wait]

    # recv local Q + apply local Q to Q0
    for r in range(diag_process, active_procs[-1] + 1):
        if local_merge_q[r][1] is not None:
            # receive q from the other processes
            local_merge_q[r][1].wait()
        if rank in active_procs:
            sum_row = sum(q_tiles.tile_rows_per_process[:r])
            end_row = q_tiles.tile_rows_per_process[r] + sum_row
            # slice of q_tiles -> [0: -> end local, 1: start -> stop]
            q_rest_loc = q_tiles.local_get(key=(slice(None), slice(sum_row, end_row)))
            # apply the local merge to q0 then update q0`
            q_rest_loc = q_rest_loc @ local_merge_q[r][0]
            q_tiles.local_set(key=(slice(None), slice(sum_row, end_row)), value=q_rest_loc)
            del local_merge_q[r]

    # global Q calculation ---------------------------------------------------------------------
    # split up the Q's from the global QR calculation and set them in a dict w/ proper keys
    global_merge_dict = (
        __global_q_dict_set(q_dict_col=q_dict[col], col=col, a_tiles=a_tiles, q_tiles=q_tiles)
        if rank == diag_process
        else {}
    )

    if rank == diag_process:
        merge_dict_keys = set(global_merge_dict.keys())
    else:
        merge_dict_keys = None
    merge_dict_keys = comm.bcast(merge_dict_keys, root=diag_process)

    # send the global merge dictionary to all processes
    for k in merge_dict_keys:
        if rank == diag_process:
            snd = global_merge_dict[k].clone()
            snd_shape = snd.shape
            comm.bcast(snd_shape, root=diag_process)
        else:
            snd_shape = None
            snd_shape = comm.bcast(snd_shape, root=diag_process)
            snd = torch.zeros(snd_shape, dtype=q0_torch_type, device=q0_torch_device)
        wait = comm.Ibcast(snd, root=diag_process)
        global_merge_dict[k] = [snd, wait]
    if rank in active_procs:
        # create a dictionary which says what tiles are in each column of the global merge Q
        qi_mult = {}
        for c in range(q_tiles.tile_columns):
            # this loop is to slice the merge_dict keys along each column + create the
            qi_mult_set = set([(i, c) for i in range(col, q_tiles.tile_columns)])
            if len(qi_mult_set & merge_dict_keys) != 0:
                qi_mult[c] = list(qi_mult_set & merge_dict_keys)

        # have all the q_merge in one place, now just do the mm with q0
        # get all the keys which are in a column (qi_mult[column])
        row_inds = q_tiles.row_indices + [q_tiles.arr.gshape[0]]
        q_copy = q_tiles.arr._DNDarray__array.clone()
        for qi_col in qi_mult.keys():
            # multiply q0 rows with qi cols
            # the result of this will take the place of the row height and the column width
            out_sz = q_tiles.local_get(key=(slice(None), qi_col)).shape
            mult_qi_col = torch.zeros(
                (q_copy.shape[1], out_sz[1]), dtype=q0_dtype.torch_type(), device=q0_torch_device
            )
            for ind in qi_mult[qi_col]:
                if global_merge_dict[ind][1] is not None:
                    global_merge_dict[ind][1].wait()
                lp_q = global_merge_dict[ind][0]
                if mult_qi_col.shape[1] < lp_q.shape[1]:
                    new_mult = torch.zeros(
                        (mult_qi_col.shape[0], lp_q.shape[1]),
                        dtype=mult_qi_col.dtype,
                        device=q0_torch_device,
                    )
                    new_mult[:, : mult_qi_col.shape[1]] += mult_qi_col.clone()
                    mult_qi_col = new_mult

                mult_qi_col[
                    row_inds[ind[0]] : row_inds[ind[0]] + lp_q.shape[0], : lp_q.shape[1]
                ] = lp_q
            temp = torch.matmul(q_copy, mult_qi_col)

            write_inds = q_tiles.get_start_stop(key=(0, qi_col))
            q_tiles.arr.lloc[:, write_inds[2] : write_inds[2] + temp.shape[1]] = temp

        if col in q_dict.keys():
            del q_dict[col]
    else:
        for ind in merge_dict_keys:
            global_merge_dict[ind][1].wait()
        if col in q_dict.keys():
            del q_dict[col]


def __r_calc_split0(a_tiles, q_dict, q_dict_waits, col_num, diag_process, not_completed_prs):
    """
    Function to do QR on one column of A. This will not merge the Q values. It is used by the
    __qr_split0 function. It uses a binary merge structure to merge the R values together. Each of
    these merges creates another Q values which must be sent to the diagonal process, these Q
    communications are stored in q_dict_waits, q_dict must be appended with these for the full Q
    calculation. For the Q calculation see __q_calc_split0.

    Parameters
    ----------
    a_tiles : tiles.SquareDiagTiles

    q_dict : Dict
        dictionary to save the calculated Q matrices to
    q_dict_waits : Dict
        dictionary to save the calculated Q matrices to which are
        not calculated on the diagonal process
    col_num : int
        the current column of the the R calculation
    diag_process : int
        rank of the process which has the tile which lies along the diagonal
    not_completed_prs : torch.Tensor
        tensor of the processes which have not yet finished calculating R

    Returns
    -------
    None
    """
    comm = a_tiles.arr.comm
    rank = comm.rank
    if rank < diag_process:
        return
    tile_rows_proc = a_tiles.tile_rows_per_process
    lcl_tile_row = 0 if rank != diag_process else col_num - sum(tile_rows_proc[:rank])

    # only work on the processes which have not computed the final result
    q_dict[col_num] = {}
    q_dict_waits[col_num] = {}

    # --------------- local QR calc --------------------------------------
    base_tile = a_tiles.local_get(key=(slice(lcl_tile_row, None), col_num))
    q1, r1 = base_tile.qr(some=False)
    q_dict[col_num]["l0"] = [q1, base_tile.shape]
    a_tiles.local_set(key=(slice(lcl_tile_row, None), col_num), value=r1)
    if col_num != a_tiles.tile_columns - 1:
        base_rest = a_tiles.local_get((slice(lcl_tile_row, None), slice(col_num + 1, None)))
        loc_rest = torch.matmul(q1.T, base_rest)
        a_tiles.local_set(key=(slice(lcl_tile_row, None), slice(col_num + 1, None)), value=loc_rest)
    # ------------- end local QR calc ------------------------------------
    rem1 = None
    rem2 = None
    offset = not_completed_prs[0]
    loop_size_remaining = not_completed_prs.clone()
    completed = False if loop_size_remaining.size()[0] > 1 else True
    procs_remaining = loop_size_remaining.size()[0]
    loop = 0
    while not completed:
        if procs_remaining % 2 == 1:
            # if the number of processes active is odd need to save the remainders
            if rem1 is None:
                rem1 = loop_size_remaining[-1]
                loop_size_remaining = loop_size_remaining[:-1]
            elif rem2 is None:
                rem2 = loop_size_remaining[-1]
                loop_size_remaining = loop_size_remaining[:-1]
        if rank not in loop_size_remaining and rank not in [rem1, rem2]:
            break  # if the rank is done then exit the loop
        # send the data to the corresponding processes
        zipped = zip(
            loop_size_remaining.flatten()[: procs_remaining // 2],
            loop_size_remaining.flatten()[procs_remaining // 2 :],
        )
        for pr in zipped:
            pr0, pr1 = int(pr[0].item()), int(pr[1].item())
            __merge_tile_rows_qr(
                pr0=pr0,
                pr1=pr1,
                column=col_num,
                rank=rank,
                a_tiles=a_tiles,
                diag_process=diag_process,
                key=str(loop) + "p0" + str(pr0) + "p1" + str(pr1) + "e",
                q_dict=q_dict,
            )

            __send_q_to_diag_pr(
                col_num=col_num,
                pr0=pr0,
                pr1=pr1,
                diag_process=diag_process,
                comm=comm,
                q_dict=q_dict,
                key=str(loop) + "p0" + str(pr0) + "p1" + str(pr1) + "e",
                q_dict_waits=q_dict_waits,
                q_dtype=a_tiles.arr.dtype.torch_type(),
                q_device=a_tiles.arr._DNDarray__array.device,
            )

        loop_size_remaining = loop_size_remaining[: -1 * (procs_remaining // 2)]
        procs_remaining = loop_size_remaining.size()[0]

        if rem1 is not None and rem2 is not None:
            # combine rem1 and rem2 in the same way as the other nodes,
            # then save the results in rem1 to be used later
            __merge_tile_rows_qr(
                pr0=rem2,
                pr1=rem1,
                column=col_num,
                rank=rank,
                a_tiles=a_tiles,
                diag_process=diag_process,
                key=str(loop) + "p0" + str(int(rem1)) + "p1" + str(int(rem2)) + "e",
                q_dict=q_dict if q_dict is not None else {},
            )

            rem1, rem2 = int(rem1), int(rem2)
            __send_q_to_diag_pr(
                col_num=col_num,
                pr0=rem2,
                pr1=rem1,
                diag_process=diag_process,
                key=str(loop) + "p0" + str(int(rem1)) + "p1" + str(int(rem2)) + "e",
                q_dict=q_dict if q_dict is not None else {},
                comm=comm,
                q_dict_waits=q_dict_waits,
                q_dtype=a_tiles.arr.dtype.torch_type(),
                q_device=a_tiles.arr._DNDarray__array.device,
            )
            rem1 = rem2
            rem2 = None

        loop += 1
        if rem1 is not None and rem2 is None and procs_remaining == 1:
            # combine rem1 with process 0 (offset) and set completed to True
            # this should be the last thing that happens
            __merge_tile_rows_qr(
                pr0=offset,
                pr1=rem1,
                column=col_num,
                rank=rank,
                a_tiles=a_tiles,
                diag_process=diag_process,
                key=str(loop) + "p0" + str(int(offset)) + "p1" + str(int(rem1)) + "e",
                q_dict=q_dict,
            )

            offset, rem1 = int(offset), int(rem1)
            __send_q_to_diag_pr(
                col_num=col_num,
                pr0=offset,
                pr1=rem1,
                diag_process=diag_process,
                key=str(loop) + "p0" + str(int(offset)) + "p1" + str(int(rem1)) + "e",
                q_dict=q_dict,
                comm=comm,
                q_dict_waits=q_dict_waits,
                q_dtype=a_tiles.arr.dtype.torch_type(),
                q_device=a_tiles.arr._DNDarray__array.device,
            )
            rem1 = None

        completed = True if procs_remaining == 1 and rem1 is None and rem2 is None else False


def __qr_split0(a, tiles_per_proc=1, calc_q=True, overwrite_a=False):
    """
    Calculates the QR decomposition of a 2D DNDarray with split == 0

    Parameters
    ----------
    a : DNDarray
        DNDarray which will be decomposed
    tiles_per_proc : int, singlt element torch.Tensor
        optional, default: 1
        number of tiles per process to operate on
    calc_q : bool
        optional, default: True
        whether or not to calculate Q
        if True, function returns (Q, R)
        if False, function returns (None, R)
    overwrite_a : bool
        optional, default: False
        if True, function overwrites the DNDarray a, with R
        if False, a new array will be created for R

    Returns
    -------
    tuple of Q and R
        if calc_q == True, function returns (Q, R)
        if calc_q == False, function returns (None, R)
    """
    if not overwrite_a:
        a = a.copy()
    a.create_square_diag_tiles(tiles_per_proc=tiles_per_proc)
    tile_columns = a.tiles.tile_columns

    q0 = factories.eye(
        (a.gshape[0], a.gshape[0]), split=0, dtype=a.dtype, comm=a.comm, device=a.device
    )
    q0.create_square_diag_tiles(tiles_per_proc=tiles_per_proc)
    q0.tiles.match_tiles(a.tiles)

    a_torch_device = a._DNDarray__array.device

    # loop over the tile columns
    rank = a.comm.rank
    active_procs = torch.arange(a.comm.size)
    empties = torch.nonzero(a.tiles.lshape_map[..., 0] == 0)
    empties = empties[0] if empties.numel() > 0 else []
    for e in empties:
        active_procs = active_procs[active_procs != e]
    tile_rows_per_pr_trmd = a.tiles.tile_rows_per_process[: active_procs[-1] + 1]

    q_dict = {}
    q_dict_waits = {}
    proc_tile_start = torch.cumsum(
        torch.tensor(tile_rows_per_pr_trmd, device=a_torch_device), dim=0
    )
    # ==================================== R Calculation ===========================================
    for col in range(tile_columns):  # for each tile column (need to do the last rank separately)
        # for each process need to do local qr
        not_completed_processes = torch.nonzero(col < proc_tile_start).flatten()
        if rank not in not_completed_processes or rank not in active_procs:
            # if the process is done calculating R the break the loop
            break
        diag_process = not_completed_processes[0]

        __r_calc_split0(
            a_tiles=a.tiles,
            q_dict=q_dict,
            q_dict_waits=q_dict_waits,
            col_num=col,
            diag_process=diag_process,
            not_completed_prs=not_completed_processes,
        )
    if not calc_q:
        # return statement if not calculating q
        a.balance_()
        return None, a
    # ===================================== Q Calculation ==========================================
    for col in range(tile_columns):
        diag_process = (
            torch.nonzero(proc_tile_start > col)[0] if col != tile_columns else proc_tile_start[-1]
        )
        diag_process = diag_process.item()

        __q_calc_split0(
            a_tiles=a.tiles,
            q_tiles=q0.tiles,
            col=col,
            q_dict=q_dict,
            q_dict_waits=q_dict_waits,
            diag_process=diag_process,
            active_procs=active_procs,
        )

    a.balance_()
    q0.balance_()
    return q0, a


def __qr_split1(a, tiles_per_proc=1, calc_q=True, overwrite_a=False):
    """
    Calculates the QR decomposition of a 2D DNDarray with split == 1

    Parameters
    ----------
    a : DNDarray
        DNDarray which will be decomposed
    tiles_per_proc : int, singlt element torch.Tensor
        optional, default: 1
        number of tiles per process to operate on
    calc_q : bool
        optional, default: True
        whether or not to calculate Q
        if True, function returns (Q, R)
        if False, function returns (None, R)
    overwrite_a : bool
        optional, default: False
        if True, function overwrites the DNDarray a, with R
        if False, a new array will be created for R

    Returns
    -------
    tuple of Q and R
        if calc_q == True, function returns (Q, R)
        if calc_q == False, function returns (None, R)
    """
    if not overwrite_a:
        a = a.copy()
    a.create_square_diag_tiles(tiles_per_proc=tiles_per_proc)
    tile_columns = a.tiles.tile_columns
    tile_rows = a.tiles.tile_rows

    q0 = factories.eye(
        (a.gshape[0], a.gshape[0]), split=0, dtype=a.dtype, comm=a.comm, device=a.device
    )
    q0.create_square_diag_tiles(tiles_per_proc=tiles_per_proc)
    q0.tiles.match_tiles(a.tiles)

    a_torch_device = a._DNDarray__array.device

    # loop over the tile columns
    proc_tile_start = torch.cumsum(
        torch.tensor(a.tiles.tile_columns_per_process, device=a_torch_device), dim=0
    )
    # ==================================== R Calculation ===========================================
    # todo: change tile columns to be the correct number here
    lp_cols = tile_columns if a.gshape[0] > a.gshape[1] else tile_rows
    for dcol in range(lp_cols):  # dcol is the diagonal column
        # loop over each column, need to do the QR for each tile in the column(should be rows)
        # need to get the diagonal process
        not_completed_processes = torch.nonzero(dcol < proc_tile_start).flatten()
        diag_process = not_completed_processes[0].item()
        # get the diagonal tile and do qr on it
        # send q to the other processes
        # 1st qr: only on diagonal tile + apply to the row
        __qr_split1_loop(
            a_tiles=a.tiles, q_tiles=q0.tiles, diag_pr=diag_process, dim0=dcol, calc_q=calc_q
        )

    # a and q0 might be purposely unbalanced during the tile matching
    a.balance_()
    if not calc_q:
        return None, a.tiles.arr
    q0.balance_()
    return q0, a


def __qr_split1_loop(a_tiles, q_tiles, diag_pr, dim0, calc_q, dim1=None):
    if dim1 is None:
        dim1 = dim0
    comm = a_tiles.arr.comm
    rank = comm.rank
    tile_rows = a_tiles.tile_rows
    a_torch_device = a_tiles.arr._DNDarray__array.device
    a_torch_type = a_tiles.arr.dtype.torch_type()
    q_torch_device = q_tiles.arr._DNDarray__array.device
    q_torch_type = q_tiles.arr.dtype.torch_type()
    if rank == diag_pr:
        q1, r1 = a_tiles[dim0, dim1].qr(some=False)
        comm.Bcast(q1.clone(), root=diag_pr)
        st_sp = a_tiles.get_start_stop(key=(dim0, dim1))
        # if rank == 3:
        #     st_sp = a_tiles.get_start_stop(key=(5, 3))

        print(dim0, dim1, st_sp, a_tiles.arr.lshape)
        a_tiles[dim0, dim1] = r1
        # apply q1 to the trailing matrix

        # need to convert dcol to a local index
        loc_col = dim1 - sum(a_tiles.tile_columns_per_process[:rank])
        hold = a_tiles.local_get(key=(dim0, slice(loc_col + 1, None)))
        if hold is not None:
            a_tiles.local_set(key=(dim0, slice(loc_col + 1, None)), value=torch.matmul(q1.T, hold))
    elif rank > diag_pr:
        # update the trailing matrix and then do q calc
        st_sp = a_tiles.get_start_stop(key=(dim0, dim1))
        sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]

        q1 = torch.zeros((sz[0], sz[0]), dtype=a_torch_type, device=a_torch_device)
        comm.Bcast(q1, root=diag_pr)
        hold = a_tiles.local_get(key=(dim0, slice(0, None)))
        a_tiles.local_set(key=(dim0, slice(0, None)), value=torch.matmul(q1.T, hold))
    else:
        # these processes are already done calculating R, only need to calc Q, but need q1
        st_sp = a_tiles.get_start_stop(key=(dim0, dim1))
        sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
        q1 = torch.zeros((sz[0], sz[0]), dtype=a_torch_type, device=a_torch_device)
        comm.Bcast(q1, root=diag_pr)

    # ======================== begin q calc for single tile QR ========================
    if calc_q:
        for row in range(q_tiles.tile_rows_per_process[rank]):
            # q1 is applied to each tile of the column dim0 of q0 then written there
            q_tiles.local_set(
                key=(row, dim0), value=torch.matmul(q_tiles.local_get(key=(row, dim0)), q1)
            )
    del q1
    # ======================== end q calc for single tile QR ==========================
    # loop over the rest of the rows, combine the tiles, then apply the result to the rest
    # 2nd step: merged QR on the rows
    # ======================== begin r calc for merged tile QR ========================
    diag_tile = a_tiles[dim0, dim1]
    st_sp = a_tiles.get_start_stop(key=(dim0, dim1))
    diag_sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
    # (Q) need to get the start stop of diag tial
    diag_st_sp = a_tiles.get_start_stop(key=(dim0, dim1))
    for row in range(dim0 + 1, tile_rows):
        if rank == diag_pr:
            # cat diag tile and loop tile
            # print(row, dim0)
            loop_tile = a_tiles[row, dim1]
            loop_cat = torch.cat((diag_tile, loop_tile), dim=0)
            # qr
            ql, rl = loop_cat.qr(some=False)
            # send ql to all
            comm.Bcast(ql.clone(), root=diag_pr)
            # set rs
            a_tiles[dim0, dim1] = rl[: diag_sz[0]]
            a_tiles[row, dim1] = rl[diag_sz[0] :]
            # apply q to rest
            loc_col = dim1 - sum(a_tiles.tile_columns_per_process[:rank])
            if loc_col + 1 < a_tiles.tile_columns_per_process[rank]:
                upp = a_tiles.local_get(key=(dim0, slice(loc_col + 1, None)))
                low = a_tiles.local_get(key=(row, slice(loc_col + 1, None)))
                hold = torch.matmul(ql.T, torch.cat((upp, low), dim=0))
                # set upper
                a_tiles.local_set(key=(dim0, slice(loc_col + 1, None)), value=hold[: diag_sz[0]])
                # set lower
                a_tiles.local_set(key=(row, slice(loc_col + 1, None)), value=hold[diag_sz[0] :])
        elif rank > diag_pr:
            st_sp = a_tiles.get_start_stop(key=(row, dim0))
            lp_sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
            ql = torch.zeros([lp_sz[0] + diag_sz[0]] * 2, dtype=a_torch_type, device=a_torch_device)
            comm.Bcast(ql, root=diag_pr)
            upp = a_tiles.local_get(key=(dim0, slice(0, None)))
            low = a_tiles.local_get(key=(row, slice(0, None)))
            hold = torch.matmul(ql.T, torch.cat((upp, low), dim=0))
            # set upper
            a_tiles.local_set(key=(dim0, slice(0, None)), value=hold[: diag_sz[0]])
            # set lower
            a_tiles.local_set(key=(row, slice(0, None)), value=hold[diag_sz[0] :])
        else:
            st_sp = a_tiles.get_start_stop(key=(row, dim0))
            lp_sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
            ql = torch.zeros([lp_sz[0] + diag_sz[0]] * 2, dtype=a_torch_type, device=a_torch_device)
            comm.Bcast(ql, root=diag_pr)
        # ========================= end r calc for merged tile QR =========================
        # ======================== begin q calc for merged tile QR ========================
        if calc_q:
            top_left = ql[: diag_sz[0], : diag_sz[0]]
            top_right = ql[: diag_sz[0], diag_sz[0] :]
            bottom_left = ql[diag_sz[0] :, : diag_sz[0]]
            bottom_right = ql[diag_sz[0] :, diag_sz[0] :]
            # two multiplications: one for the left tiles and one for the right
            # left tiles --------------------------------------------------------------------
            # create a column of the same size as the tile row of q0
            st_sp = a_tiles.get_start_stop(key=(slice(dim0, None), dim1))
            qloop_col_left_sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
            qloop_col_left = torch.zeros(
                qloop_col_left_sz, dtype=q_torch_type, device=q_torch_device
            )
            # top left starts at 0 and goes until diag_sz[1]
            qloop_col_left[: diag_sz[0]] = top_left
            # bottom left starts at ? and goes until ? (only care about 0th dim)
            st, sp, _, _ = a_tiles.get_start_stop(key=(row, 0))
            st -= diag_st_sp[0]  # adjust these by subtracting the start index of the diag tile
            sp -= diag_st_sp[0]
            qloop_col_left[st:sp] = bottom_left
            # right tiles --------------------------------------------------------------------
            # create a columns tensor of the size of the tile column of index 'row'
            st_sp = q_tiles.get_start_stop(key=(row, slice(dim1, None)))
            sz = st_sp[1] - st_sp[0], st_sp[3] - st_sp[2]
            qloop_col_right = torch.zeros(sz[1], sz[0], dtype=q_torch_type, device=q_torch_device)
            # top left starts at 0 and goes until diag_sz[1]
            qloop_col_right[: diag_sz[0]] = top_right
            # bottom left starts at ? and goes until ? (only care about 0th dim)
            st, sp, _, _ = a_tiles.get_start_stop(key=(row, 0))
            st -= diag_st_sp[0]  # adjust these by subtracting the start index of the diag tile
            sp -= diag_st_sp[0]
            qloop_col_right[st:sp] = bottom_right
            for qrow in range(q_tiles.tile_rows_per_process[rank]):
                # print(qrow, dcol)
                # print(qrow, row)
                # q1 is applied to each tile of the column dcol of q0 then written there
                q0_row = q_tiles.local_get(key=(qrow, slice(dim0, None))).clone()
                q_tiles.local_set(key=(qrow, dim0), value=torch.matmul(q0_row, qloop_col_left))
                q_tiles.local_set(key=(qrow, row), value=torch.matmul(q0_row, qloop_col_right))
        del ql
        # ======================== end q calc for merged tile QR ==========================


def __send_q_to_diag_pr(
    col_num, pr0, pr1, diag_process, comm, q_dict, key, q_dict_waits, q_dtype, q_device
):
    """
    This function sends the merged Q to the diagonal process. Buffered send it used for sending
    Q. This is needed for the Q calculation when two processes are merged and neither is the diagonal
    process.

    Parameters
    ----------
    col_num : int
        The current column used in the parent QR loop
    pr0, pr1 : int, int
        Rank of processes 0 and 1. These are the processes used in the calculation of q
    diag_process : int
        The rank of the process which has the tile along the diagonal for the given column
    comm : MPICommunication (ht.DNDarray.comm)
        The communicator used. (Intended as the communication of the DNDarray 'a' given to qr)
    q_dict : Dict
        dictionary containing the Q values calculated for finding R
    key : string
        key for q_dict[col] which corresponds to the Q to send
    q_dict_waits : Dict
        Dictionary used in the collection of the Qs which are sent to the diagonal process
    q_dtype : torch.type
        Type of the Q tensor
    q_device : torch.Device
        Device of the Q tensor

    Returns
    -------
    None, sets the values of q_dict_waits with the with *waits* for the values of Q, upper.shape,
        and lower.shape
    """
    if comm.rank not in [pr0, pr1, diag_process]:
        return
    # this is to send the merged q to the diagonal process for the forming of q
    base_tag = "1" + str(pr1.item() if isinstance(pr1, torch.Tensor) else pr1)
    if comm.rank == pr1:
        q = q_dict[col_num][key][0]
        u_shape = q_dict[col_num][key][1]
        l_shape = q_dict[col_num][key][2]
        comm.send(tuple(q.shape), dest=diag_process, tag=int(base_tag + "1"))
        comm.Isend(q, dest=diag_process, tag=int(base_tag + "12"))
        comm.send(u_shape, dest=diag_process, tag=int(base_tag + "123"))
        comm.send(l_shape, dest=diag_process, tag=int(base_tag + "1234"))
    if comm.rank == diag_process:
        # q_dict_waits now looks like a
        q_sh = comm.recv(source=pr1, tag=int(base_tag + "1"))
        q_recv = torch.zeros(q_sh, dtype=q_dtype, device=q_device)
        k = "p0" + str(pr0) + "p1" + str(pr1)
        q_dict_waits[col_num][k] = []
        q_wait = comm.Irecv(q_recv, source=pr1, tag=int(base_tag + "12"))
        q_dict_waits[col_num][k].append([q_recv, q_wait])
        q_dict_waits[col_num][k].append(comm.irecv(source=pr1, tag=int(base_tag + "123")))
        q_dict_waits[col_num][k].append(comm.irecv(source=pr1, tag=int(base_tag + "1234")))
        q_dict_waits[col_num][k].append(key[0])
