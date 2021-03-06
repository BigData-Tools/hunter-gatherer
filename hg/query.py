import re
import sys
import subprocess
import tempfile

from parser import parse_into_chunks as parse_query

def str_to_parsed_query(query):
    phrases = re.findall('\(.+?\)', query)
    result = []
    for phrase in phrases:
        phrase = phrase.strip()[1:-1]
        pos = phrase.find(',')
        phrase_type = phrase[:pos].strip()
        words = map(lambda word: word.strip()[1:-1].lower(), phrase[pos+1:].strip()[1:-1].split(','))
        result.append( ( phrase_type, words) )
    return result
    
def generate_indri_query(query, passage_len, passage_inc):
    sub_queries = []
    query_terms = []
    for phrase in query:
        phrase_type = phrase[0]
        words = phrase[1]
        word_query = ' '.join(words)
        phrase_query = '#1(%s)' % word_query
        if phrase_type == 'NE':
            sub_queries.append(phrase_query)
            query_terms.append(word_query)
        elif phrase_type == 'Non-NE':
            sub_queries.append('#combine(0.5 %s 0.5 %s)' % (phrase_query, word_query))
            query_terms.append(word_query)
            query_terms += words
        elif phrase_type == 'None' or phrase_type is None:
            sub_queries.append(word_query)
            query_terms += words
    indri_query = '#combine[passage%d:%d](%s)' % (int(passage_len), int(passage_inc), ' '.join(sub_queries))
    return indri_query, query_terms

def generate_param_file(index_path, query, res_num, query_terms):
    f = tempfile.NamedTemporaryFile(delete=True)
    f.write(index_path + '\n')
    f.write(query + '\n')
    f.write(str(res_num) + '\n')
    for query_term in query_terms:
        f.write(query_term + '\n')
    f.flush()
    return f

if __name__ == '__main__':
    option = sys.argv[1]
    argv = sys.argv[2:]
    if option == '--example':
        indri_query, query_terms = generate_indri_query(str_to_parsed_query("[ (NE, ['Mexican', 'Food']), (Non-NE, ['little', 'wonder']), (None, ['strong']) ]"), 50, 20)
        f = generate_param_file('../data/index', indri_query, 3, query_terms)
        subprocess.call(['cpp/Search', f.name])
        f.close()
    elif option == '--search' or option == '--search-with-parsed-query':
        index_path, search_file, query_or_parsed, passage_len, passage_inc, res_num = argv
        if option == '--search':
            query = parse_query(query_or_parsed)
        else:
            query = str_to_parsed_query(query_or_parsed)
        indri_query, query_terms = generate_indri_query(query, passage_len, passage_inc)
        f = generate_param_file(index_path, indri_query, res_num, query_terms)
        subprocess.call(['%s' % search_file, f.name])
        f.close()
        
