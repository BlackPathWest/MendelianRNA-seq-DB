#!/usr/bin/python3
#!/usr/bin/env bash

import os
import sys
import argparse
import multiprocessing
import subprocess
import sqlite3
import re
from datetime import datetime

databasePath = ""

def connectToDB():
	conn = sqlite3.connect('SpliceJunction.db', timeout=20)
	cur = conn.cursor()

	return conn, cur

def commitAndClose(conn):
	conn.commit()
	conn.close()

def initializeDB():

	conn, cur = connectToDB()

	cur.execute('''PRAGMA journal_mode=WAL;''')

	# if not ('wal' in cur.fetchone()):
	# 	print("Could not set SQLite database to WAL mode. Exiting.")
	# 	exit(1)

	cur.execute('''PRAGMA foreign_keys = ON;''')

	cur.execute('''create table if not exists SAMPLE_REF (
		sample_name varchar(50) primary key, 
		type tinyint not null);''') # type = {0, 1} 
									# GTEX, patient

	cur.execute('''create table if not exists JUNCTION_REF (
		chromosome tinyint not null,
		start unsigned big int not null,
		stop unsigned big int not null,
		gencode_annotation tinyint not null,
		n_patients_seen unsigned big int default 0,
		n_gtex_seen unsigned big int default 0,
		total_read_count big int default 0,
		primary key (chromosome, start, stop));''') # gencode_annotation = {0, 1, 2, 3, 4}
													# none, only start, only stop, both, exon skipping

	cur.execute('''create table if not exists JUNCTION_COUNTS (
		bam_id integer not null,
		junction_id integer not null,
		read_count unsigned big int not null,
		norm_read_count float,
		foreign key(bam_id) references SAMPLE_REF(ROWID),
		foreign key(junction_id) references JUNCTION_REF(ROWID),
		primary key (bam_id, junction_id));''')

	cur.execute('''create table if not exists GENE_REF (
		gene varchar(30) not null,
		junction_id integer not null,
		foreign key(junction_id) references JUNCTION_REF(ROWID)
		primary key (gene, junction_id));''')

	# not needed, since a b-tree of (chromosome, start, stop) will be used for (chromosome) and (chromosome, start)
	# https://stackoverflow.com/questions/795031/how-do-composite-indexes-work
	# cur.execute('''create index startJunction
	# 	on JUNCTION_REF (chromosome, start);
	# 	''')

	cur.execute('''create index stopJunction
		on JUNCTION_REF (chromosome, stop);
		''')

	cur.execute('''create unique index wholeJunction
		on JUNCTION_REF (chromosome, start, stop);
		''')

	cur.execute('''create unique index sample_junction
		on JUNCTION_COUNTS (bam_id, junction_id);
		''')

	commitAndClose(conn)

def getJunctionID(cur, chrom, start, stop):

	# gencode_annotation = {0, 1, 2, 3, 4}
	# none, only start, only stop, both, exon skipping
	# thus, gencode junctions will always have a gencode_annotation value of 3

	# check if start and stop are apart of an existing gencode annotation
	cur.execute('''select ROWID, gencode_annotation from JUNCTION_REF where 
		chromosome is ? and 
		start is ? and 
		stop is ?;''', (chrom, start, stop))
	res = cur.fetchone()

	if res:
		ROWID, annotation = res
	else: # if no such junction determine annotation of new junction: novel junction, only one annotated or a case of exon skipping?
		
		cur.execute('''select * from JUNCTION_REF where 
			gencode_annotation is 3 and 
			chromosome is ? and 
			start is ?;''', (chrom, start))
		isStartAnnotated = cur.fetchone()

		cur.execute('''select * from JUNCTION_REF where 
			gencode_annotation is 3 and 
			chromosome is ? and 
			stop is ?;''', (chrom, stop))
		isStopAnnotated = cur.fetchone()

		if isStopAnnotated and isStartAnnotated:
			annotation = 4 # exon skipping
		elif isStopAnnotated:
			annotation = 2 # only stop
		elif isStartAnnotated:
			annotation = 1 # only start
		else:
			annotation = 0 # novel junction

		try:
			cur.execute('''insert into JUNCTION_REF (
				chromosome, 
				start, 
				stop, 
				gencode_annotation) 
				values (?, ?, ?, ?);''', (chrom, start, stop, annotation))

			ROWID = cur.lastrowid
		except sqlite3.IntegrityError: # if another worker process has inserted the same junction in between this code block's execution, then just return the junction_id from the database
			cur.execute('''select ROWID, gencode_annotation from JUNCTION_REF where 
			chromosome is ? and 
			start is ? and 
			stop is ?;''', (chrom, start, stop))

			ROWID, annotation = cur.fetchone()
		
	return ROWID, annotation

def makeSpliceDict(bam, gene_file):

	spliceDict = {}

	with open(gene_file, "r") as gf:
		for line in gf:

			chrom, start, stop, count = line.strip().split()
			uniqueSplice = (chrom, start, stop) 

			spliceDict[uniqueSplice] = int(count)

	return spliceDict

def normalizeReadCount(spliceDict, junction, annotation, annotated_counts):

	# gencode_annotation = {0, 1, 2, 3, 4}
	# none, only start, only stop, both, exon skipping

	chrom, start, stop = junction
	annotation = int(annotation)

	if (annotation == 0) or (annotation == 4): 
		return 'NULL'
	elif annotation == 1:
		key = makeStartString(chrom, start)
	elif annotation == 2:
		key = makeStopString(chrom, stop)
	elif annotation == 3:
		startString = makeStartString(chrom, start)
		stopString = makeStopString(chrom, stop)				

		if annotated_counts[startString] > annotated_counts[stopString]:
			key = startString
		else:
			key = stopString

	res = round((float(spliceDict[junction]) / float(annotated_counts[key])), 3)

	return str(res)

def makeStartString(chrom, start):
	return ''.join([chrom,':','START',':',start])

def makeStopString(chrom, stop):
	return ''.join([chrom,':','STOP',':',stop])

def get_annotated_counts(spliceDict):

	count_dict = {}

	for junction in spliceDict:

		chrom, start, stop = junction

		startString = makeStartString(chrom, start)
		stopString = makeStopString(chrom, stop)

		if startString in count_dict:
			if count_dict[startString] < spliceDict[junction]:
				count_dict[startString] = spliceDict[junction]
		else:
			count_dict[startString] = spliceDict[junction]

		if stopString in count_dict:
			if count_dict[stopString] < spliceDict[junction]:
				count_dict[stopString] = spliceDict[junction]
		else:
			count_dict[stopString] = spliceDict[junction]

	return count_dict

def summarizeGeneFile(poolArguement):

	bamList, gene = poolArguement
	conn, cur = connectToDB()

	print ('processing ' + gene)

	for bam in bamList:

		bam_id, bam_type = get_bam_id_and_type(cur, bam)

		sample = bam[:-4]
		gene_file = ''.join([os.getcwd(), "/", sample, "/", gene, ".txt"])

		if not os.path.isfile(gene_file):
			continue

		spliceDict = makeSpliceDict(sample, gene_file)
		annotated_counts = get_annotated_counts(spliceDict)

		for junction in spliceDict:

			chrom, start, stop = junction
			reads = spliceDict[junction]

			junction_id, annotation = getJunctionID(cur, chrom, start, stop)

			try:
				norm_read_count = normalizeReadCount(spliceDict, junction, annotation, annotated_counts)
			except ZeroDivisionError:
				print("Zero division error when normalizing %s:%s-%s in genefile %s.txt in sample %s with annotation %d"%(chrom, start, stop, gene, sample, annotation))
				norm_read_count = 'null'

			# check if gene is related to junction, add it if not
			annotateJunctionWithGene(gene, junction_id, cur)

			lock.acquire()
			updateJunctionInformation(junction_id, bam_id, bam_type, gene, sample, reads, norm_read_count, cur)
			lock.release()

		del spliceDict, annotated_counts
		
	commitAndClose(conn)

	print ('finished ' + gene)

def updateJunctionInformation(junction_id, bam_id, bam_type, gene, sample, new_read_count, new_norm_read_count, cur):

	# check if sample already has the junction in the database
	cur.execute('''select ROWID, read_count from JUNCTION_COUNTS where junction_id is ? and bam_id is ?;''', (junction_id, bam_id))
	res = cur.fetchone()

	# if it is, check if new_reads > old_reads, update JUNCTION_REF and JUNCTION_COUNTS for the appropriate sample
	if res:
		sample_junction_id, old_read_count = res

		if int(new_read_count) > int(old_read_count):

			# update entry to reflect new read count values
			cur.execute('''update JUNCTION_COUNTS set read_count = ?, norm_read_count = ? where ROWID = ?;''', (new_read_count, new_norm_read_count, sample_junction_id))

			# update total read counts
			cur.execute('''update JUNCTION_REF set total_read_count = total_read_count - ? + ? where ROWID = ?;''', (old_read_count, new_read_count, junction_id))

	# if not, add it with read counts and normalized read counts, increment n_samples_seen, increment n_times_seen, increment JUNCTION_REF total times seen
	else:
		cur.execute('''insert into JUNCTION_COUNTS (bam_id, junction_id, read_count, norm_read_count) values (?, ?, ?, ?);''', (bam_id, junction_id, new_read_count, new_norm_read_count))

		# 0 = gtex, 1 = patient
		if bam_type == 1:
			cur.execute('''update JUNCTION_REF set n_patients_seen = n_patients_seen + 1, total_read_count = total_read_count + ? where ROWID = ?;''', (new_read_count, junction_id))
		elif bam_type == 0:
			cur.execute('''update JUNCTION_REF set n_gtex_seen = n_gtex_seen + 1, total_read_count = total_read_count + ? where ROWID = ?;''', (new_read_count, junction_id))

def get_bam_id_and_type(cur, bam):
	cur.execute('''select ROWID, type from SAMPLE_REF where sample_name = ?;''', (bam, ))
	bam_id, bam_type = cur.fetchone()

	return bam_id, bam_type

def makeLockGlobal(poolLock):
	global lock
	lock = poolLock

def parallel_process_gene_files(num_processes, bam_files, gencode_file, gene_list):

	conn, cur = connectToDB()

	bamList = []
	poolArguements = []
	gene_set = set()
	poolLock = multiprocessing.Lock()

	with open(bam_files, "r") as bf:
		for line in bf:

			bam = line.strip()

			try: # insert sample names into SAMPLE_REF
				if 'GTEX' in bam:
					cur.execute('''insert into SAMPLE_REF (sample_name, type) values (?, 0);''', (bam, ))
				else: # sample is a patient
					cur.execute('''insert into SAMPLE_REF (sample_name, type) values (?, 1);''', (bam, ))
			except sqlite3.IntegrityError as e:
				continue # if sample already in DB, don't process it

			bamList.append(bam) # only process samples if insert was successful 

	commitAndClose(conn)

	# get only 1 instance of each gene, sets can only contain unique elements
	with open(gene_list, "r") as gf:
		for line in gf:
			gene = line.strip().split()[0]

			gene_set.add(gene)

	for gene in gene_set:
		poolArguements.append((bamList, gene))

	print ("Creating a pool with " + str(num_processes) + " processes")
	pool = multiprocessing.Pool(initializer=makeLockGlobal, initargs=(poolLock, ), processes=int(num_processes), maxtasksperchild=1000)
	print ('pool: ' + str(pool))

	pool.map(summarizeGeneFile, poolArguements)
	pool.close()
	pool.join()

def annotateJunctionWithGene(gene, junction_id, cur):
	# assign the junction to the gene specified
	cur.execute('''insert or ignore into GENE_REF (gene, junction_id) values (?, ?);''', (gene, junction_id))

def addTranscriptModelJunction(chrom, start, stop, gene, cur):

	try:
		cur.execute('''insert into JUNCTION_REF (chromosome, start, stop, gencode_annotation) values (?, ?, ?, ?);''', (chrom, start, stop, '3')) # 3 stands for both annotated
		junction_id = cur.lastrowid
	except sqlite3.IntegrityError:
		cur.execute('''select ROWID from JUNCTION_REF where chromosome = ? and start = ? and stop = ?;''', (chrom, start, stop))
		junction_id = cur.fetchone()[0]

	annotateJunctionWithGene(gene, junction_id, cur)

def storeTranscriptModelJunctions(gencode_file, enableFlanking):

	initializeDB()
	conn, cur = connectToDB()

	print ('Started adding transcript_model junctions @ ' + datetime.now().strftime("%Y-%m-%d_%H:%M:%S.%f"))

	with open(gencode_file, "r") as gf:
		for commitFreq, line in enumerate(gf):

			chrom, start, stop, gene = line.strip().split()[0:4]

			start = int(start)
			stop = int(stop)

			if enableFlanking:

				# shifts the junction while maintaining the same distance between start and stop
				for offset in range(-1,2):
					startFlank = start + offset
					stopFlank = stop + offset
					addTranscriptModelJunction(chrom, startFlank, stopFlank, gene, cur)

				# generate junctions with the most extreme flanking regions of start and stop
				addTranscriptModelJunction(chrom, (start + 1), (stop - 1), gene, cur)
				addTranscriptModelJunction(chrom, (start - 1), (stop + 1), gene, cur)

				# generate junctions with a 1 off start or stop
				# fixed start
				addTranscriptModelJunction(chrom, start, (stop - 1), gene, cur)
				addTranscriptModelJunction(chrom, start, (stop + 1), gene, cur)
				# fixed stop
				addTranscriptModelJunction(chrom, (start - 1), stop, gene, cur)
				addTranscriptModelJunction(chrom, (start + 1), stop, gene, cur)

			else:
				addTranscriptModelJunction(chrom, start, stop, gene, cur)

			if commitFreq % 500 == 0: #	yes this works
				conn.commit()

	commitAndClose(conn)

	print ('Finished adding gencode annotations @ ' + datetime.now().strftime("%Y-%m-%d_%H:%M:%S.%f"))

if __name__=="__main__":

	print ('SpliceJunctionSummary.py started on ' + datetime.now().strftime("%Y-%m-%d_%H:%M:%S.%f"))

	parser = argparse.ArgumentParser(description = 'Summarize the read counts of the junctions reported by SpliceJunctionDiscovery.py')
	parser.add_argument('-transcript_model',help="Transcript model of canonical splicing, e.g. gencode v19. Default is set to /home/dennis.kao/tools/MendelianRNA-seq-DB/gencode.comprehensive.splice.junctions.txt",action='store',default = "/home/dennis.kao/largeWork/gene-lists/all-protein-coding-genes-no-patches.list")
	parser.add_argument('-processes',help='Number of worker processes to parse gene files, default=10.',default=10)
	parser.add_argument('-bamlist',help='A text file containing the names of bam files you want to discover splice junctions in each on a seperate line, default=bamlist.list',default='bamlist.list')
	parser.add_argument('-gene_list',help='A text file containing the names of all the genes you want to investigate, default=gene_list.txt',default='gene_list.txt')
	parser.add_argument('-db',help='The name of the database you are storing junction information in, default=SpliceJunction.db',default='SpliceJunction.db')
	mode_arguments = parser.add_mutually_exclusive_group(required=True)
	mode_arguments.add_argument('--addGencode',action='store_true',help='Populate the database with gencode junctions, this step needs to be done once before anything else')
	mode_arguments.add_argument('--addGencodeWithFlanks',action='store_true',help='Populate the database with gencode junctions with a +/- 1 nucleotide range, this step needs to be done once before anything else')
	mode_arguments.add_argument('--addBAM',action='store_true',help='Add junction information from bamfiles found in the file bamlist.list')
	args=parser.parse_args()

	print ('Working in directory ' + str(os.getcwd()))

	databasePath = args.db

	if args.addGencode:
		print ('Storing junctions from the transcript model file ' + args.transcript_model)
		storeTranscriptModelJunctions(args.transcript_model, False)
	elif args.addGencodeWithFlanks:
		print ('Storing junctions with +/- 1 flanks from the transcript model file ' + args.transcript_model)
		storeTranscriptModelJunctions(args.transcript_model, True)
	elif args.addBAM:
		print ('Storing junctions from bam files found in the file ' + args.bamlist)
		parallel_process_gene_files(args.processes, args.bamlist, args.transcript_model, args.gene_list)

	print ('SpliceJunctionSummary.py finished on ' + datetime.now().strftime("%Y-%m-%d_%H:%M:%S.%f"))
