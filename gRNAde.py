import dotenv
dotenv.load_dotenv(".env")

import os
import random
import argparse
import numpy as np
from typing import Optional

import torch
import torch.nn.functional as F
import torch_geometric

from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord

from src.trainer import evaluate, self_consistency_score
from src.data.featurizer import RNAGraphFeaturizer
from src.models import AutoregressiveMultiGNNv1
from src.data.data_utils import get_backbone_coords
from src.constants import (
    NUM_TO_LETTER, 
    RNA_ATOMS, 
    FILL_VALUE,
    PROJECT_PATH
)


# Model checkpoint paths corresponding to maximum no. of conformers
CONFORMERS_TO_CHECKPOINT_PATH = {
    1: os.path.join(PROJECT_PATH, "checkpoints/gRNAde_ARv1_1state.h5"),
    2: os.path.join(PROJECT_PATH, "checkpoints/gRNAde_ARv1_2state.h5"),
    3: os.path.join(PROJECT_PATH, "checkpoints/gRNAde_ARv1_3state.h5"),
    5: os.path.join(PROJECT_PATH, "checkpoints/gRNAde_ARv1_5state.h5"),
}

# Default model hyperparameters (do not change)
VERSION = 0.2
RADIUS = 0.0
TOP_K = 32
NUM_RBF = 32
NUM_POSENC = 32
NOISE_SCALE = 0.1
NODE_IN_DIM = (15, 4)
NODE_H_DIM = (128, 16)
EDGE_IN_DIM = (131, 3)
EDGE_H_DIM = (64, 4)
NUM_LAYERS = 4
DROP_RATE = 0.5
OUT_DIM = 4
DEFAULT_N_SAMPLES = 16
DEFAULT_TEMPERATURE = 0.1


class gRNAde(object):
    """
    gRNAde: a Geometric Deep Learning pipeline for 3D RNA Inverse Design.

    This class loads a gRNAde inverse folding model checkpoint corresponding 
    to a maximum number of conformers and allows the user to perform fixed 
    backbone re-design of RNA structures.

    Args:
        max_num_conformers (int): maximum number of conformers for an input RNA backbone
        gpu_id (int): GPU ID to use for inference (defaults to cpu if no GPU is available)
    """

    def __init__(
            self,
            max_num_conformers: Optional[int] = 1,
            gpu_id: Optional[int] = 0,
        ):

        # Set version
        self.version = VERSION
        print(f"Instantiating gRNAde v{self.version}")

        # Set maximum number of conformers
        if max_num_conformers > max(list(CONFORMERS_TO_CHECKPOINT_PATH.keys())):
            max_num_conformers = max(list(CONFORMERS_TO_CHECKPOINT_PATH.keys()))
            print(f"    Invalid max_num_conformers. Setting to maximum value: {max_num_conformers}")
        self.max_num_conformers = max_num_conformers
        
        # Set device (GPU/CPU)
        device = torch.device("cuda:{}".format(gpu_id) if torch.cuda.is_available() else "cpu")
        print(f"    Using device: {device}")
        self.device = device

        # Define data featurizer
        print(f"    Creating RNA graph featurizer for max_num_conformers={max_num_conformers}")
        self.featurizer = RNAGraphFeaturizer(
            split = "test",  # set to 'train' to use noise augmentation
            radius = RADIUS,
            top_k = TOP_K,
            num_rbf = NUM_RBF,
            num_posenc = NUM_POSENC,
            max_num_conformers = max_num_conformers,
            noise_scale = NOISE_SCALE
        )

        # Initialise model
        print(f"    Initialising GNN encoder-decoder model")
        self.model = AutoregressiveMultiGNNv1(
            node_in_dim = NODE_IN_DIM,
            node_h_dim = NODE_H_DIM, 
            edge_in_dim = EDGE_IN_DIM,
            edge_h_dim = EDGE_H_DIM, 
            num_layers = NUM_LAYERS,
            drop_rate = DROP_RATE,
            out_dim = OUT_DIM
        )
        # Load model checkpoint
        self.model_path = CONFORMERS_TO_CHECKPOINT_PATH[max_num_conformers]
        print(f"    Loading model checkpoint: {self.model_path}")
        self.model.load_state_dict(torch.load(self.model_path, map_location=torch.device('cpu')))

        # Transfer model to device in eval mode
        self.model = self.model.to(device)
        self.model.eval()

        print(f"Finished initialising gRNAde v{self.version}\n")

    def design_from_pdb_file(
            self, 
            pdb_filepath,
            output_filepath: Optional[str] = None, 
            n_samples: Optional[int] = DEFAULT_N_SAMPLES,
            temperature: Optional[float] = DEFAULT_TEMPERATURE,
            seed: Optional[int] = 0
        ):
        """
        Design RNA sequences for a PDB file, i.e. fixed backbone re-design
        of the RNA structure.

        Args:
            pdb_filepath (str): filepath to PDB file
            output_filepath (str): filepath to write designed sequences to
            n_samples (int): number of samples to generate
            temperature (float): temperature for sampling
            seed (int): random seed for reproducibility
        
        Returns:
            sequences (List[SeqRecord]): designed sequences in fasta format
            samples (Tensor): designed sequences with shape `(n_samples, seq_len)`
            perplexity (Tensor): perplexity per sample with shape `(n_samples, 1)`
            recovery (Tensor): sequence recovery per sample with shape `(n_samples, 1)`
            sc_score (Tensor): global self consistency score per sample with shape `(n_samples, 1)`
        """
        featurized_data, raw_data = self.featurizer.featurize_from_pdb_file(pdb_filepath)
        return self.design(raw_data, featurized_data, output_filepath, n_samples, temperature, seed)

    def design_from_directory(
            self,
            directory_filepath,
            output_filepath: Optional[str] = None, 
            n_samples: Optional[int] = DEFAULT_N_SAMPLES,
            temperature: Optional[float] = DEFAULT_TEMPERATURE,
            seed: Optional[int] = 0
        ):
        """
        Design RNA sequences for directory of PDB files corresponding to the 
        same RNA molecule, i.e. fixed backbone re-design given multiple
        conformations of the RNA structure.

        Args:
            directory_filepath (str): filepath to directory of PDB files
            output_filepath (str): filepath to write designed sequences to
            n_samples (int): number of samples to generate
            temperature (float): temperature for sampling
            seed (int): random seed for reproducibility
        
        Returns:
            sequences (List[SeqRecord]): designed sequences in fasta format
            samples (Tensor): designed sequences with shape `(n_samples, seq_len)`
            perplexity (Tensor): perplexity per sample with shape `(n_samples, 1)`
            recovery (Tensor): sequence recovery per sample with shape `(n_samples, 1)`
            sc_score (Tensor): global self consistency score per sample with shape `(n_samples, 1)`
        """
        pdb_filelist = []
        for pdb_filepath in os.listdir(directory_filepath):
            if pdb_filepath.endswith(".pdb"):
                pdb_filelist.append(os.path.join(directory_filepath, pdb_filepath))
        featurized_data, raw_data = self.featurizer.featurize_from_pdb_filelist(pdb_filelist)
        return self.design(raw_data, featurized_data, output_filepath, n_samples, temperature, seed)

    @torch.no_grad()
    def design(
        self, 
        raw_data: dict, 
        featurized_data: Optional[torch_geometric.data.Data] = None, 
        output_filepath: Optional[str] = None, 
        n_samples: Optional[int] = DEFAULT_N_SAMPLES,
        temperature: Optional[float] = DEFAULT_TEMPERATURE,
        seed: Optional[int] = 0
    ):
        """
        Design RNA sequences from raw data.

        Args:
            raw_data (dict): Raw RNA data dictionary with keys:
                - sequence (str): RNA sequence of length `num_res`.
                - coords_list (Tensor): Backbone coordinates with shape
                    `(num_conf, num_res, num_bb_atoms, 3)`.
                - sec_struct_list (List[str]): Secondary structure for each
                    conformer in dotbracket notation.
            featurized_data (torch_geometric.data.Data): featurized RNA data
            output_filepath (str): filepath to write designed sequences to
            n_samples (int): number of samples to generate
            temperature (float): temperature for sampling
            seed (int): random seed for reproducibility
        
        Returns:
            sequences (List[SeqRecord]): designed sequences in fasta format
            samples (Tensor): designed sequences with shape `(n_samples, seq_len)`
            perplexity (Tensor): perplexity per sample with shape `(n_samples, 1)`
            recovery (Tensor): sequence recovery per sample with shape `(n_samples, 1)`
            sc_score (Tensor): global self consistency score per sample with shape `(n_samples, 1)`
        """
        # set random seed
        set_seed(seed)

        if raw_data['coords_list'][0].shape[1] == 3:
            # Expected input: num_conf x num_res x num_bb_atoms x 3
            # Backbone atoms: (P, C4', N1 or N9)
            pass
        elif raw_data['coords_list'][0].shape[1] == len(RNA_ATOMS):
            coords_list = []
            for coords in raw_data['coords_list']:
                # Only keep backbone atom coordinates: num_res x num_bb_atoms x 3
                coords = get_backbone_coords(coords, raw_data['sequence'])
                # Do not add structures with missing coordinates for ALL residues
                if not torch.all((coords == FILL_VALUE).sum(axis=(1,2)) > 0):
                    coords_list.append(coords)

            if len(coords_list) > 0:
                # Add processed coords_list to self.data_list
                raw_data['coords_list'] = coords_list
        else:
            raise ValueError(f"Invalid number of atoms per nucleotide in input data: {raw_data['coords_list'][0].shape[1]}")

        if featurized_data is None:
            # featurize raw data
            featurized_data = self.featurizer.featurize(raw_data)

        # transfer data to device
        featurized_data = featurized_data.to(self.device)
        
        # sample n_samples from model for single data point: n_samples x seq_len
        samples, logits = self.model.sample(
            featurized_data, n_samples, temperature, return_logits=True)

        # perplexity per sample: n_samples x 1
        n_nodes = logits.shape[1]
        perplexity = torch.exp(F.cross_entropy(
            logits.view(n_samples * n_nodes, self.model.out_dim), 
            samples.view(n_samples * n_nodes).long(), 
            reduction="none"
        ).view(n_samples, n_nodes).mean(dim=1)).cpu().numpy()
        
        # sequence recovery per sample: n_samples x 1
        recovery = samples.eq(featurized_data.seq).float().mean(dim=1).cpu().numpy()

        # global self consistency score per sample: n_samples x 1
        sc_score = self_consistency_score(
            samples.cpu().numpy(), 
            raw_data['sec_struct_list'], 
            featurized_data.mask_coords.cpu().numpy()
        )

        # collate designed sequences in fasta format
        sequences = [
            # first record: input sequence and model metadata
            SeqRecord(
                Seq(raw_data["sequence"]),
                id=f"input_sequence,",
                description=f"gRNAde_version={self.version}, model={self.model.__class__.__name__}, max_num_conformers={self.max_num_conformers}, checkpoint={self.model_path}, seed={seed}"
            )
        ]
        # remaining records: designed sequences and metrics
        for idx, zipped in enumerate(zip(
            samples.cpu().numpy(),
            perplexity,
            recovery,
            sc_score
        )):
            seq, perp, rec, sc = zipped
            seq = "".join([NUM_TO_LETTER[num] for num in seq])
            sequences.append(SeqRecord(
                Seq(seq), 
                id=f"sample={idx},",
                description=f"temperature={temperature}, perplexity={perp:.4f}, recovery={rec:.4f}, sc_score={sc:.4f}"
            ))
        
        if output_filepath is not None:
            # write sequences to output filepath
            SeqIO.write(sequences, output_filepath, "fasta")

        return sequences, samples, perplexity, recovery, sc_score


def set_seed(seed=0):
    """
    Sets random seed for reproducibility.
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--pdb_filepath', 
        dest='pdb_filepath', 
        default=None,
        type=str,
        help="Filepath to PDB file to be re-designed (single-state design)"
    )
    parser.add_argument(
        '--directory_filepath', 
        dest='directory_filepath', 
        default=None,
        type=str,
        help="Filepath to directory of PDB files to be re-designed, \
            corresponding to the same RNA molecule (multi-state design)"
    )
    parser.add_argument(
        '--output_filepath', 
        dest='output_filepath', 
        default=None,
        type=str, 
        help="Filepath to fasta file to save designed sequences"
    )
    parser.add_argument(
        '--max_num_conformers', 
        dest='max_num_conformers', 
        default=1, 
        type=int,
        help="Maximum number of conformers for input RNA backbone (multi-state design)"
    )
    parser.add_argument(
        '--n_samples', 
        dest='n_samples', 
        default=16, 
        type=int,
        help="Number of samples to generate"
    )
    parser.add_argument(
        '--temperature', 
        dest='temperature', 
        default=0.2, 
        type=float,
        help="Temperature for sampling"
    )
    parser.add_argument(
        '--seed', 
        dest='seed', 
        default=0, 
        type=int,
        help="Random seed for reproducibility"
    )
    parser.add_argument(
        '--gpu_id', 
        dest='gpu_id', 
        default=0, 
        type=int,
        help="GPU ID to use for inference \
            (defaults to cpu if no GPU is available)"
    )
    args, unknown = parser.parse_known_args()

    if args.pdb_filepath is None and args.directory_filepath is None:
        raise ValueError("Please specify either pdb_filepath or directory_filepath")

    g = gRNAde(
        max_num_conformers=args.max_num_conformers, 
        gpu_id=args.gpu_id
    )

    if args.pdb_filepath is not None:
        sequences, samples, logits, recovery_sample, sc_score = g.design_from_pdb_file(
            pdb_filepath=args.pdb_filepath,
            output_filepath=args.output_filepath,
            n_samples=args.n_samples,
            temperature=args.temperature,
            seed=args.seed
        )
    elif args.directory_filepath is not None:
        sequences, samples, logits, recovery_sample, sc_score = g.design_from_directory(
            directory_filepath=args.directory_filepath,
            output_filepath=args.output_filepath,
            n_samples=args.n_samples,
            temperature=args.temperature,
            seed=args.seed
        )

    for seq in sequences:
        print(seq.format("fasta"))
    