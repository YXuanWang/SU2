import os
import shutil
from enum import IntEnum

import numpy as np
from mpi4py import MPI

import pysu2
import pysu2ad


class RunCode(IntEnum):
    """Run codes for communication with worker processes."""
    STOP = -1
    RUN_FORWARD = 0
    RUN_ADJOINT = 1


def run_forward(comm, forward_driver, inputs):
    """Runs a simulation with the provided driver, using the inputs to set the values
    defined in DIFF_INPUTS in the config file.

    :param comm: The communicator for the processes running the simulation.
    :param forward_driver: The driver for the simulation, created using the same comm as passed into this function.
    :param inputs: The inputs used to set the DIFF_INPUTS as defined in the configuration file.
    :return: The outputs of the simulation, as defined in DIFF_OUTPUTS in the config file.
    """
    for i, x in enumerate(inputs):
        forward_driver.SetDiff_Inputs_Vars(x.flatten().tolist(), i)
    forward_driver.ApplyDiff_Inputs_Vars()

    forward_driver.StartSolver()
    comm.Barrier()
    # TODO Way to get results in-memory, without writing to file?
    if comm.Get_rank() == 0 and os.path.isfile('./restart_flow.dat'):
        shutil.move('./restart_flow.dat', './solution_flow.dat')

    # are we using numpy or torch
    is_numpy = len(inputs) == 0 or type(inputs[0]) is np.ndarray
    if is_numpy:
        array_func = np.array
        cat_func = np.concatenate
    else:
        import torch
        array_func = inputs[0].new_tensor
        cat_func = torch.cat

    num_diff_outputs = forward_driver.GetnDiff_Outputs()
    outputs = [array_func(forward_driver.GetDiff_Outputs_Vars(i))
               for i in range(num_diff_outputs)]

    for i in range(num_diff_outputs):
        if outputs[i].shape[0] > 1:
            # if dealing with full-grid, reorder according to GlobalIndex
            if comm.Get_size() > 1:
                # gather outputs in rank 0 if more than one rank
                outputs[i] = comm.gather(outputs[i], root=0)
                global_inds = comm.gather(forward_driver.GetAllGlobalIndices(), root=0)
                if comm.Get_rank() == 0:
                    outputs[i] = cat_func(outputs[i])
                    global_inds = list(sum(global_inds, tuple()))  # join tuples
            else:
                global_inds = list(forward_driver.GetAllGlobalIndices())

            if comm.Get_rank() == 0:
                # TODO Make the list integers on the C side
                global_inds = np.array(global_inds, dtype=np.long)
                assert outputs[i].shape[0] == len(global_inds), \
                    'Only full grid outputs supported by now (besides scalars).'
                # order by global_inds
                outputs[i][global_inds] = outputs[i].copy() if is_numpy else outputs[i].clone()
            else:
                outputs[i] = None
    return tuple(outputs)


def run_adjoint(comm, adjoint_driver, inputs, grad_outputs):
    """Runs a simulation with the provided driver, using the inputs to set the values
    defined in DIFF_INPUTS in the config file.

    :param comm: The communicator for the processes running the simulation.
    :param adjoint_driver: The driver for the adjoint computation, created using the same comm as passed into this function.
    :param inputs: The same inputs used to set the DIFF_INPUTS in the forward pass.
    :param grad_outputs: Gradients of a scalar loss with respect to the forward outputs, see SU2Function's backward() method.
    :return: The gradients of the loss with respect to the inputs.
    """
    for i, x in enumerate(inputs):
        adjoint_driver.SetDiff_Inputs_Vars(x.flatten().tolist(), i)
    adjoint_driver.ApplyDiff_Inputs_Vars()
    for i, g in enumerate(grad_outputs):
        adjoint_driver.SetBackprop_Derivs(g.flatten().tolist(), i)

    adjoint_driver.StartSolver()
    # comm.Barrier()

    # are we using numpy or torch
    is_numpy = len(inputs) == 0 or type(inputs[0]) is np.ndarray
    array_func = np.array if is_numpy else inputs[0].new_tensor
    grads = None
    if comm.Get_rank() == 0:
        grads = tuple(array_func(adjoint_driver.GetTotal_Sens_Diff_Inputs(i))
                      for i in range(adjoint_driver.GetnDiff_Inputs()))
    return grads


def main():
    """Runs a loop for the worker processes.
    Can be signaled to run either a forward simulation or an adjoint computation
    using RunCodes.
    """
    intercomm = MPI.Comm.Get_parent()
    num_zones, dims = intercomm.bcast(None, root=0)

    inputs = None
    while True:
        run_type = intercomm.bcast(None, root=0)
        if run_type == RunCode.STOP:
            break
        config = intercomm.bcast(None, root=0)

        if run_type == RunCode.RUN_FORWARD:
            inputs = intercomm.bcast(None, root=0)
            forward_driver = pysu2.CSinglezoneDriver(config, num_zones, dims, MPI.COMM_WORLD)
            outputs = run_forward(MPI.COMM_WORLD, forward_driver, inputs)
            if MPI.COMM_WORLD.Get_rank() == 0:
                intercomm.send(outputs, dest=0)
            forward_driver.Postprocessing()

        elif run_type == RunCode.RUN_ADJOINT:
            assert inputs is not None, 'Run forward simulation before running the adjoint.'
            grad_outputs = intercomm.bcast(None, root=0)
            adjoint_driver = pysu2ad.CDiscAdjSinglezoneDriver(config, num_zones, dims, MPI.COMM_WORLD)
            grads = run_adjoint(MPI.COMM_WORLD, adjoint_driver, inputs, grad_outputs)
            if MPI.COMM_WORLD.Get_rank() == 0:
                intercomm.send(grads, dest=0)
            adjoint_driver.Postprocessing()

    intercomm.Disconnect()


if __name__ == '__main__':
    # will run this when run with mpirun
    main()
