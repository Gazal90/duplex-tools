"""
The purpose of this script is to split reads that are likely to be informatic concatamers.
These reads typically have an imperfect match to two neighbouring adapter sequences found next to each other.

A simplified view is the following:

Here is a read, where ">>" represents the adapter sequence and "=" represent bases from the organism of interest.
The reverse adapter can typically be found in the end of a read, represented as "<<" below

A typical read, with adapters in the ends, and bases of interest in the middle

    >>=====<<

Here is a chimeric read that has not been split in the software.
        A        B
    >>=====<<>>=====<<

B can either be complementary to A, in which case they form a follow-on pair, which formed dsDNA prior to sequencing.
B can also be genomically distant from A, in which case it is may also desirable to split this read to avoid chimaeras

This module will look for matches to "<<>>" like below, and split these reads out into separate parts.

Before:

    read_id: 0ae195a2-6993-4a0b-afa8-bb834f4739e3
            A        B
        >>=====<<>>=====<<
               ^--^

After:

    read_id: 0ae195a2-6993-4a0b-afa8-bb834f4739e3_1
            A
        >>=====<<

    read_id: 0ae195a2-6993-4a0b-afa8-bb834f4739e3_2
            B
        >>=====<<

"""

from concurrent.futures import ProcessPoolExecutor
import functools
import gzip
from pathlib import Path
import pickle
import sys

import edlib
import fire
from natsort import natsorted
import numpy as np
from pyfastx import Fastx
from tqdm import tqdm

rev_comp = lambda seq: str.translate(seq, str.maketrans('ACGT', 'TGCA'))[::-1]

EDIT_THRESHOLDS = {'PCR': 45,
                   'Native': 9}

mask_size_default_head = 5
mask_size_default_tail = 14
mask_size_default_N = 11

HEAD_ADAPTER = 'AATGTACTTCGTTCAGTTACGTATTGCT'
TAIL_ADAPTER = 'GCAATACGTAACTGAACGAAGT'


def build_targets(n_bases_to_mask_head,
                  n_bases_to_mask_tail,
                  degenerate_bases=None,
                  pcr_primers=('ACTTGCCTGTCGCTCTATCTTCGGCGTCTGCTTGGGTGTTTAACC',
                               'TTTCTGTTGGTGCTGATATTGCGGCGTCTGCTTGGGTGTTTAACCT'),
                  head_adapter=HEAD_ADAPTER,
                  tail_adapter=TAIL_ADAPTER,
                  n_replacement=None):
    if degenerate_bases is None:
        degenerate_bases = n_bases_to_mask_head + n_bases_to_mask_tail

    if n_replacement is None:
        middle_sequence = 'N' * degenerate_bases
    else:
        middle_sequence = n_replacement

    targets = {
        'Native': [tail_adapter[:len(tail_adapter)-n_bases_to_mask_tail] +
                   middle_sequence +
                   head_adapter[n_bases_to_mask_head:]],
        'PCR': [rev_comp(x) +
                tail_adapter[:-n_bases_to_mask_tail] +
                middle_sequence +
                head_adapter[n_bases_to_mask_head:] + y
                for x in pcr_primers for y in pcr_primers]
    }
    return targets


def find_mid_adaptor(seq, targets, print_alignment=False, print_threshold=10, print_id=None):
    trim = 200
    seq = seq[trim:-trim]  # remove start and end adaptor
    results = [edlib.align(target, seq, mode="HW", task="path",
                           additionalEqualities=(('N', 'A'), ('N', 'C'), ('N', 'G'), ('N', 'T'))) for target in targets]
    if print_alignment:
        alignments = [edlib.getNiceAlignment(result, target, seq) for result, target in zip(results, targets) if
                      result['cigar'] is not None]
        for alignment, result in zip(alignments, results):
            if result['editDistance'] < print_threshold:
                print(f"{print_id} editdistance-{result['editDistance']}")
                print("\n".join(alignment.values()))

    i = np.argmin([x['editDistance'] for x in results])
    res = results[i]
    if res['cigar'] is not None:
        res['locations'] = [(x + trim, y + trim) for (x, y) in res['locations']]
    return res


def write_match_to_fasta(file, seq, start, end, read_id):
    matchlen = end - start
    matchlen_half = matchlen // 2
    seq_left = seq[(start + matchlen_half - 100):start]
    seq_mid = seq[start:end]
    seq_right = seq[end:(end - matchlen_half + 100)]
    fullseq = ''.join([seq_left, seq_mid, seq_right])
    if len(fullseq) > 0:
        file.write(f'>{read_id}\n{fullseq}\n')


def deduplicate_locations_first_key(result):
    result['locations'] = sorted(list({x: y for x, y in reversed(result['locations'])}.items()))
    return result


def process_file(
        fastx, targets, output_dir=None,
        type="Native",
        n_bases_to_mask_tail=mask_size_default_tail,
        n_bases_to_mask_head=mask_size_default_head,
        degenerate_bases=mask_size_default_N,
        debug=False,
        edit_threshold=None,
        n_replacement=None,
        print_alignment=False, print_threshold_delta=0):
    newfastx = fastx.with_name(fastx.stem.split('.')[0] + '_split').with_suffix(fastx.suffixes[0])
    if output_dir is not None:
        newfastx = Path(output_dir) / newfastx.name
    if debug:
        newfasta = fastx.with_name(fastx.stem.split('.')[0] + '_middle').with_suffix('.fasta')
        fasta = open(newfasta, 'w')

    edited_reads = set()
    unedited_reads = set()
    split_multiple_times = set()
    with gzip.open(newfastx, mode='wt', compresslevel=1) as outfh:
        for read_id, seq, qual, comments in tqdm(Fastx(str(fastx)), leave=False):
            result = find_mid_adaptor(seq, targets,
                                      print_alignment=print_alignment,
                                      print_threshold=edit_threshold + print_threshold_delta,
                                      print_id=read_id)

            if result['editDistance'] < edit_threshold:
                result = deduplicate_locations_first_key(result)
                if len(result['locations']) > 1:
                    outfh.write(f'@{read_id} {comments}\n{seq}\n+\n{qual}\n')
                    split_multiple_times.add(read_id)
                    continue

                edited_reads.add(read_id)
                [(start, end)] = result['locations']
                if debug:
                    write_match_to_fasta(fasta, seq, start, end, read_id)
                left_seq, right_seq = seq[:start], seq[end:]
                left_qual, right_qual = qual[:start], qual[end:]
                outfh.write(f'@{read_id}_1 {comments}\n{left_seq}\n+\n{left_qual}\n')
                outfh.write(f'@{read_id}_2 {comments}\n{right_seq}\n+\n{right_qual}\n')
            else:
                outfh.write(f'@{read_id} {comments}\n{seq}\n+\n{qual}\n')
                unedited_reads.add(read_id)
    if debug:
        fasta.close()
    return edited_reads, unedited_reads, split_multiple_times


def split(fastq_dir,
         output_dir=None,
         type="Native",
         n_bases_to_mask_tail=mask_size_default_tail,
         n_bases_to_mask_head=mask_size_default_head,
         degenerate_bases=mask_size_default_N,
         debug=False,
         edit_threshold=None,
         n_replacement=None,
         pattern='*.fastq.gz', print_alignment=False, print_threshold_delta=0,
         threads=None):
    """

    :param fastq_dir: The directory from which to search for fastq/fasta files to split
    :param output_dir: Output directory for fastq.
    :param type: The type of sample, either Native or PCR
    :param n_bases_to_mask_tail: Number of bases to mask from the tail adapter (number of bases at the end of read)
    :param n_bases_to_mask_head: Number of bases to mask from the head adapter (number of bases at the start of read)
    :param degenerate_bases: count of N's between tail and head adapter (defaults to n_bases_to_mask_tail + n_bases_to_mask_head)
    :param debug: Whether to output additional files helpful for debugging.
    :param edit_threshold: The threshold at which to split reads. Reads with edit distance below this value will be split
    :param n_replacement: Optional sequence to use as replacement of the masked bases. Can be used if the call is
    :param pattern: The pattern to use for matching fastq/fasta files.
    :param print_alignment: Whether to pretty-print the alignment at each match.
    :param print_threshold_delta: The threshold to print the alignment at, relative to edit_threshold
    :param threads: number of worker threads.
    """
    fastxs = natsorted(list(Path(fastq_dir).rglob(pattern)), key=str)
    if output_dir is not None:
        output = Path(output_dir)
        try:
            output.mkdir()
        except FileExistsError:
            print("The output directory should not pre-exist.")
            sys.exit(1)

    targets = build_targets(n_bases_to_mask_head=n_bases_to_mask_head,
                            n_bases_to_mask_tail=n_bases_to_mask_tail,
                            degenerate_bases=degenerate_bases,
                            n_replacement=n_replacement)[type]
    if edit_threshold is None:
        edit_threshold = EDIT_THRESHOLDS[type]
    edited_reads = set()
    unedited_reads = set()
    split_multiple_times = set()
    worker = functools.partial(
        process_file,
        targets=targets, output_dir=output_dir,
        type=type, n_bases_to_mask_tail=n_bases_to_mask_tail, n_bases_to_mask_head=n_bases_to_mask_head,
        degenerate_bases=degenerate_bases, debug=debug, edit_threshold=edit_threshold,
        n_replacement=n_replacement, print_alignment=print_alignment,
        print_threshold_delta=print_threshold_delta)

    with ProcessPoolExecutor(max_workers=threads) as executor:
        results = executor.map(worker, fastxs)
        for edited, unedited, multi in tqdm(results):
            edited_reads.update(edited)
            unedited_reads.update(unedited)
            split_multiple_times.update(multi)

    with open('edited.pkl', 'wb') as handle:
        pickle.dump(edited_reads, handle)
    with open('unedited.pkl', 'wb') as handle:
        pickle.dump(unedited_reads, handle)
    with open('split_multiple_times.pkl', 'wb') as handle:
        pickle.dump(split_multiple_times, handle)
    nedited_reads = len(edited_reads)
    nunedited_reads = len(unedited_reads)
    print(f'Split {nedited_reads} reads kept {nunedited_reads} reads')


def main():
    fire.Fire(split)