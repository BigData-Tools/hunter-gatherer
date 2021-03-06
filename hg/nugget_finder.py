import sys
import os
import re
import subprocess
import nltk
import numpy as np

from shutil import copyfile
from subprocess import Popen, PIPE, STDOUT
from operator import itemgetter
from multiprocessing import Pool

from query import generate_indri_query, generate_param_file
from parser import parse_into_chunks
from html_to_trec import detag_html_file
from pattern import parse_pattern_chunks
from candidate_scorer import CandidateScorer, Searcher
import fastmap

USE_PATTERNS = False
USE_CANDIDATE_SCORER = False

def do_search(query, search_command, path_to_index, num_passages):
    
    indri_query, query_terms = generate_indri_query(query,120, 50)
    f = generate_param_file(path_to_index, indri_query, num_passages, query_terms)
    
    cmd = 'cpp/Search %s' % (f.name)
    #sys.stderr.write('Searching %s in file %s\n' % (indri_query, f.name))
    p = Popen(cmd, shell=True, stdin=PIPE, stdout=PIPE, stderr=sys.stderr, close_fds=True)
    output = p.stdout.read()

    passages = []
    lines = output.splitlines()

    def parse_score_line(l):
        parts = l.split(' ')
        return { 'document': parts[0], 'score' : float(parts[1]), 'pos': int(parts[2])-1 }

    result = []
    line_idx = 2 # drop index and query lines
    #sys.stderr.write('Got %d lines\n' % (len(lines),))
    while line_idx < len(lines):
        line = lines[line_idx]; line_idx += 1
        parts = line.split(" ")
        score_line = parse_score_line(line)

        line = lines[line_idx]; line_idx += 1
        assert line == 'passage-content:'
        
        text_lines = []
        while lines[line_idx] != "----------":
            text_lines.append(lines[line_idx])
            line_idx += 1

        line_idx += 1 # skip separator
        line = lines[line_idx]; line_idx += 1
        assert line == 'matched-terms:'
        terms = dict()
        while lines[line_idx] != "==========":
            line = lines[line_idx]; line_idx += 1
            query_terms, indices = line.split(':')
            indices = map(lambda x:tuple(x.split('-')), indices.split(' '))
            terms[query_terms] = indices
        
        line_idx += 1 # skip separator
        current_passage = (score_line, "\n".join(text_lines), terms)
        result.append(current_passage)

    #sys.stderr.write('Got %d results\n' % (len(result),))

    return result

def identify_candidates(passages, main_passage_count, top_documents):
    potential_candidates = dict()
    stopwords = nltk.corpus.stopwords.words('english')

    # parse all passages
    seen_documents = set()
    #print len(passages)
    processed_passages = 0
    for idx in xrange(0, len(passages)):
        if int(passages[idx][0]['document']) > top_documents:
            continue
        if passages[idx][0]['document'] in seen_documents:
            continue
        if processed_passages > main_passage_count:
            break
        processed_passages += 1
        seen_documents.add(passages[idx][0]['document'])
        passage_text = passages[idx][1]
        chunks = []
        if USE_PATTERNS:
            chunks += parse_pattern_chunks(passage_text)
        chunks +=  parse_into_chunks(passage_text)
        passage_counted = set()
        for chunk in chunks:
            chunk = (chunk[0], map(lambda x: re.sub('[^A-Za-z0-9]',' ', x).strip(), chunk[1]))
            as_str = " ".join(chunk[1]).strip()

            if as_str.lower() in stopwords:
                continue
            
            info = potential_candidates.get(as_str, dict())
            info['tokens'] = chunk[1]
            info['type'] = 'NE' if info.get('type', 'Non-NE') == 'NE' else chunk[0] # once NE, always NE
            info['score'] = passages[idx][0]['score'] + 100.0 + info.get('score', 0.0)
            info['total_count'] = 1 + info.get('total_count', 1)
            if not as_str in passage_counted:
                info['passage_count'] = 1 + info.get('passage_count', 1)
                evidence = info.get('passages', list())
                evidence.append(passages[idx])
                info['passages'] = evidence
                passage_counted.add(as_str)

            potential_candidates[as_str] = info
        #print '=== passage =>', re.sub('\s+', ' ', passages[idx][1]), '|=== candidates =>', passage_counted

    # keep all NEs plus "important" Non-NEs (might need corpus frequencies for that)
    result_candidates = []
    result_evidence = dict()
    for potential in potential_candidates:
        entry = potential_candidates[potential]
        if True: # entry['type'] == 'NE' or entry['passage_count'] > entry['total_count'] * 0.8:
            #print entry['type'], potential
            result_candidates.append( (potential, entry['tokens']) )
            result_evidence[potential] = \
                                       { 'type' : entry['type'],
                                         'score' : entry['score'],
                                         'passages': entry['passages'] }
    return (result_candidates, result_evidence)

def score_candidate(candidate, evidence, main_evidence, query):
    score = 0.0

    for entry in main_evidence.get('passages', []):
        score_line = entry[0]
        score += 100.0 + score_line['score']

    for entry in evidence:
        score_line = entry[0]
        score += 100.0 + score_line['score']

    return score * (1.0 if main_evidence.get('type', 'Non-NE') == 'NE' else 1.0)


class PoolScorer:
    def __init__(self, ini):
        if USE_CANDIDATE_SCORER:
            self.scorer = CandidateScorer(ini)
        self.count = 0

    def __call__(self, instance):    
        score = 0
        candidate, evidence, main_evidence, parsed_query = instance
        if USE_CANDIDATE_SCORER:
            score = self.scorer.score(*instance)
        else:
            score = score_candidate(candidate, evidence, main_evidence, parsed_query)
        return candidate, score, evidence

def find_nuggets(ini, htmls, query_str):
    tmp_folder = ini.get('tmp_folder', './tmp')

    ####
    # extract text from the HTML documents
    #
    sys.stderr.write("Extracting text...\n")
    path_to_corpus = "%s/to_index" % (tmp_folder,)
    if not os.path.exists(path_to_corpus):
        os.makedirs(path_to_corpus)
        
    html_count = 0
    for html in htmls:
        outfile = "%s/%s.txt" % (path_to_corpus, html_count)
        cached_detag = "%s.txt" % (html,)
        if os.path.exists(cached_detag):
            copyfile(cached_detag, outfile)
        else:
            detag_html_file(infile=html,outfile=outfile,id=html_count)
            copyfile(outfile, cached_detag)
        html_count += 1

    ####
    # build index
    #
    sys.stderr.write("Indexing...\n")
    path_to_index = "%s/index" % (tmp_folder,)
    if not os.path.exists(path_to_index):
        os.makedirs(path_to_index)
        
    config_template = file(ini.get('index_config_template', "./indexing.template")).read()
    config_filename = "%s/indexing.param" % (tmp_folder,)
    config_file = open(config_filename, "w")
    config_file.write(config_template.format(path_to_corpus=path_to_corpus,
                                             path_to_index=path_to_index))
    config_file.close()
    index_command = ini.get('index_command', 'IndriBuildIndex')

    retcode=subprocess.call([ index_command, config_filename ], stdout=sys.stderr, stderr=sys.stderr)
    assert retcode==0

    ####
    # generate query
    #
    parsed_query = parse_into_chunks(query_str)

    if bool(ini.get('condition_baseline', '')):
        print "baseline run."
        return ([], parsed_query, path_to_index)

    ####
    # main search
    #
    sys.stderr.write("Main search...\n")
    search_command = ini.get('search_command', './cpp/Search')    
    main_passages = do_search(parsed_query, search_command, path_to_index, 2000)

    ####
    # identify candidates
    #
    sys.stderr.write("Identifying candidates...\n")
    top_documents = int(ini.get('top_documents_for_candidate', '20'))
    candidates, main_evidence = identify_candidates(main_passages,
                                                    int(ini.get('main_search_passage_count', 3)),
                                                    top_documents)

    ###
    # evidence search
    #
    sys.stderr.write("Evidence searching...\n")
    evidence = dict()
    #for candidate in candidates:
        #extended_query = list(parsed_query)
        #extended_query.append( ('NE', candidate[1] ) )
        #evidence_passages = do_search(extended_query, search_command, path_to_index,
                                      #int(ini.get('evidence_search_passage_count', 10)))
        #evidence[candidate[0]] = filter(lambda passage: 
                                        #all(map(lambda token: token.lower() in passage[1].lower(), 
                                                #candidate[1])), evidence_passages)
        #sys.stderr.write('Found %d passages\n' % (len(evidence[candidate[0]]),))


    searcher = Searcher(search_command, path_to_index,
                                      int(ini.get('evidence_search_passage_count', 10)))
    print 'candidate num:%d' % len(candidates)
    queries = map(lambda candidate: list(parsed_query) + [('NE', candidate[1] )], candidates)
    evidence_passages_list = fastmap.fastmap(searcher, 10, queries)
    for i in xrange(len(candidates)):
        candidate = candidates[i]
        evidence[candidate[0]] = filter(lambda passage: 
                                        all(map(lambda token: token.lower() in passage[1].lower(), candidate[1])), evidence_passages_list[i])


    ####
    # evaluate evidence
    #
    sys.stderr.write("Evaluating evidence...\n")
    scored_candidates = list()

    if USE_CANDIDATE_SCORER: 
        pool_scorer = PoolScorer(ini)
        p = Pool(8)
        scorerd_candidates = fastmap.fastmap(pool_scorer, map(lambda candidate: (candidate, evidence[candidate], main_evidence[candidate], parsed_query), evidence.keys()))
        p.close()
    else:
        for candidate in evidence:
            scored_candidates.append( (candidate,
                                       score_candidate(candidate,
                                                       evidence[candidate],
                                                       main_evidence[candidate],
                                                       parsed_query),
                                       evidence[candidate]) )
    ####
    # clean up
    #
    if False:
        for i in xrange(0, html_count):
            try:
                os.unlink("%s/to_index/%s.txt" % (tmp_folder, i))
            except:
                pass

    ####
    # show candidates
    #
    if False:
        scored_candidates.sort(key=itemgetter(1), reverse=True)
        rank = 0;
        for candidate_score in scored_candidates:
            candidate, score, evidence = candidate_score
            print candidate
            print '\t', rank, score
            #printed = set()
            #for entry in evidence:
            #    if not entry[0]['document'] in printed:
            #        print entry[0]['document'], entry[0]['score']
            #        printed.add(entry[0]['document'])
            #        print ""
            rank += 1
 

    return (scored_candidates, parsed_query, path_to_index)

def load_ini(ini_file):
    ini = dict()
    for line in file(ini_file):
        if line[0] != '#':
            parts = line.split('=',1)
            ini[parts[0].strip()] = parts[1].strip()
    return ini


if __name__ == '__main__':
    ####
    # input: ini file, file containing the list of HTML documents and query
    #
    ini_file = sys.argv[1]
    htmls_file = sys.argv[2]
    query_str = " ".join(sys.argv[3:])

    ini = load_ini(ini_file)
    htmls = []
    html_urls = []
    for line in file(htmls_file):
        line = line.strip()
        parts = line.split('\t')
        htmls.append(parts[0])
        html_urls.append(parts[1] if len(parts) > 1 else parts[0])

    (scored_candidates, parsed_query, path_to_index) = find_nuggets(ini, htmls, query_str)
    
    ####
    # show candidates
    #
    scored_candidates.sort(key=itemgetter(1), reverse=True)
    rank = 0;
    for candidate_score in scored_candidates:
        candidate, score, evidence = candidate_score
        print candidate
        print rank
        print score
        printed = set()
        for entry in evidence:
            if not entry[0]['document'] in printed:
                print entry[0]['document'], entry[0]['score'], html_urls[int(entry[0]['document'])]
                printed.add(entry[0]['document'])
        print ""
        rank += 1
