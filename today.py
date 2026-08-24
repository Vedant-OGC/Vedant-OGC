import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN', '')
HEADERS = {'authorization': 'token ' + ACCESS_TOKEN} if ACCESS_TOKEN else {}
USER_NAME = os.environ.get('USER_NAME', 'Vedant-OGC')
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    try:
        request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS, timeout=20)
    except requests.exceptions.RequestException as e:
        raise Exception(f"{func_name} request error: {e}")
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with status code', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """
    Uses GitHub's GraphQL v4 API to return my total repository or star count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasattr: hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    try:
        request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS, timeout=25)
    except requests.exceptions.RequestException as e:
        print(f"Warning: Network error fetching LOC for {owner}/{repo_name}: {e}", flush=True)
        return addition_total, deletion_total, my_commits

    if request.status_code == 200:
        repo_data = request.json().get('data', {}).get('repository')
        if repo_data and repo_data.get('defaultBranchRef'):
            target = repo_data['defaultBranchRef'].get('target')
            if target and target.get('history'):
                return loc_counter_one_repo(owner, repo_name, data, cache_comment, target['history'], addition_total, deletion_total, my_commits)
        return addition_total, deletion_total, my_commits

    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception("Too many requests in a short amount of time! Hit GitHub anti-abuse rate limit.")
    print(f"Warning: recursive_loc failed with status {request.status_code} for {owner}/{repo_name}", flush=True)
    return addition_total, deletion_total, my_commits


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time) 
    only adds the LOC value of commits authored by me
    """
    for node in history.get('edges', []):
        author_info = node.get('node', {}).get('author')
        user_info = author_info.get('user') if author_info else None
        if user_info and user_info.get('id') == OWNER_ID.get('id'):
            my_commits += 1
            addition_total += node.get('node', {}).get('additions', 0)
            deletion_total += node.get('node', {}).get('deletions', 0)

    page_info = history.get('pageInfo', {})
    if not history.get('edges') or not page_info.get('hasattr') and not page_info.get('hasNextPage'):
        return addition_total, deletion_total, my_commits
    
    end_cursor = page_info.get('endCursor')
    if end_cursor:
        return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, end_cursor)
    return addition_total, deletion_total, my_commits


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """
    Uses GitHub's GraphQL v4 API to query all repositories with pagination
    """
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    repos_data = request.json().get('data', {}).get('user', {}).get('repositories', {})
    page_info = repos_data.get('pageInfo', {})
    current_edges = repos_data.get('edges', [])

    if page_info.get('hasNextPage'):
        edges += current_edges
        return loc_query(owner_affiliation, comment_size, force_cache, page_info.get('endCursor'), edges)
    else:
        return cache_builder(edges + current_edges, comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository to see if it has been updated since last cached.
    """
    cached = True
    os.makedirs('cache', exist_ok=True)
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        if index >= len(data):
            break
        split_line = data[index].split()
        repo_hash = split_line[0] if len(split_line) > 0 else ''
        commit_count = split_line[1] if len(split_line) > 1 else '0'
        repo_name_with_owner = edges[index]['node']['nameWithOwner']
        
        if repo_hash == hashlib.sha256(repo_name_with_owner.encode('utf-8')).hexdigest():
            try:
                branch_ref = edges[index]['node'].get('defaultBranchRef')
                if branch_ref and branch_ref.get('target'):
                    current_commits = branch_ref['target']['history']['totalCount']
                    if int(commit_count) != current_commits:
                        print(f"Scanning updated repository: {repo_name_with_owner} ({current_commits} commits)", flush=True)
                        owner, repo_name = repo_name_with_owner.split('/')
                        loc = recursive_loc(owner, repo_name, data, cache_comment)
                        data[index] = f"{repo_hash} {current_commits} {loc[2]} {loc[0]} {loc[1]}\n"
                else:
                    data[index] = f"{repo_hash} 0 0 0 0\n"
            except (TypeError, KeyError, ValueError) as e:
                print(f"Note on {repo_name_with_owner}: {e}", flush=True)
                data[index] = f"{repo_hash} 0 0 0 0\n"

    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)

    for line in data:
        loc = line.split()
        if len(loc) >= 5:
            loc_add += int(loc[3])
            loc_del += int(loc[4])
            
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes and resets the cache file
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def add_archive():
    """
    Parses repository_archive.txt for historic contributions
    """
    if not os.path.exists('cache/repository_archive.txt'):
        return [0, 0, 0, 0, 0]
    with open('cache/repository_archive.txt', 'r') as f:
        data = f.readlines()
    if len(data) <= 10:
        return [0, 0, 0, 0, 0]
    old_data = data
    data = data[7:len(data) - 3]
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        parts = line.split()
        if len(parts) >= 5:
            repo_hash, total_commits, my_commits, *loc = parts
            added_loc += int(loc[0])
            deleted_loc += int(loc[1])
            if my_commits.isdigit():
                added_commits += int(my_commits)
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]


def force_close_file(data, cache_comment):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print(f'Partial data saved to {filename}', flush=True)


def stars_counter(data):
    total_stars = 0
    for node in data:
        stargazers = node.get('node', {}).get('stargazers')
        if stargazers:
            total_stars += stargazers.get('totalCount', 0)
    return total_stars


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data)
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map.get(just_len, '')
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    if not os.path.exists(filename):
        return 0
    with open(filename, 'r') as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        parts = line.split()
        if len(parts) >= 3 and parts[2].isdigit():
            total_commits += int(parts[2])
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    user_res = request.json()['data']['user']
    return {'id': user_res['id']}, user_res['createdAt']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    time_str = ('%.4f s ' % difference) if difference > 1 else ('%.4f ms' % (difference * 1000))
    print(f"   {query_type:<20}: {time_str:>12}", flush=True)
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    print('Starting Profile Stats Calculation for:', USER_NAME, flush=True)
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2008, 5, 7))
    formatter('age calculation', age_time)
    
    print('Querying lines of code across repositories...', flush=True)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)' if total_loc[-1] else 'LOC (no cache)', loc_time)
    
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    total_loc_formatted = [f"{x:,}" if isinstance(x, int) else str(x) for x in total_loc[:-1]]

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc_formatted)
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc_formatted)

    total_time = user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time
    print(f"\nTotal execution time: {total_time:.4f} s", flush=True)
    print(f"Total GitHub GraphQL API calls: {sum(QUERY_COUNT.values())}", flush=True)
    for funct_name, count in QUERY_COUNT.items():
        print(f"   {funct_name:<25}: {count:>6}", flush=True)
    print("Done! SVGs updated successfully.", flush=True)