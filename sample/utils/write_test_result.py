import re
import codecs
import csv
import datetime
import os
import pickle as pkl
import string
from urllib.error import URLError
from collections import OrderedDict
import numpy as np
import word2vec
from tqdm import tqdm
import xml.dom.minidom
import xml.dom.minidom
from xml.dom.minidom import parse
from bioservices import UniProt
from Bio import Entrez
from keras.models import load_model
import tensorflow as tf
from keras.backend.tensorflow_backend import set_session
from keras.preprocessing.sequence import pad_sequences
# import Levenshtein  # pip install python-Levenshtein
from sklearn.preprocessing import StandardScaler
from sample.utils.helpers import get_stop_dic, pos_surround
from sample.utils.helpers import makeEasyTag, Indent, postprocess, cos_sim, extract_id_from_res
u = UniProt(cache=True)

# GPU内存分配
config = tf.ConfigProto()
config.gpu_options.per_process_gpu_memory_fraction = 0.3  # 按比例
# config.gpu_options.allow_growth = True  # 自适应分配
set_session(tf.Session(config=config))


def strippingAlgorithm(entity):
    '''实体拓展'''
    # lower-cased
    entity_variants1 = entity.lower().strip('\n').strip()
    entity_variants2 = entity.lower().strip('\n').strip()

    # punctuation-removed
    for punc in string.punctuation:
        if punc in entity_variants1:
            entity_variants1 = entity_variants1.replace(punc, ' ')
            entity_variants2 = entity_variants2.replace(punc, ' ' + punc + ' ')

    entity_variants1 = entity_variants1.replace('  ', ' ').strip()
    entity_variants2 = entity_variants2.replace('  ', ' ').strip()

    # remove common words
    common = ['protein', 'proteins', 'gene', 'genes', 'rna', 'organism']
    for com in common:
        if com in entity_variants1:
            entity_variants1 = entity_variants1.replace(com, '').strip()
        if com in entity_variants2:
            entity_variants2 = entity_variants2.replace(com, '').strip()

    # 分离实体中的字母和数字
    entity_variants3 = re.findall(r'[0-9]+|[a-z]+', entity_variants2)
    entity_variants3 = ' '.join(entity_variants3)
    entity_variants3 = entity_variants3.replace('  ', ' ').strip()

    return entity_variants2, entity_variants1, entity_variants3


def get_test_out_data(path):
    '''
    获取测试集所有句子list的集合
    '''
    sen_line = []
    sen_list = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line == '\n':
                sen_list.append(sen_line)
                sen_line = []
            else:
                token = line.replace('\n', '').split('\t')
                sen_line.append(token[0])
    return sen_list


def getCSVData(csv_path):
    '''
    获取实体ID词典 {'entity':[id1, id2, ...]}
    '''
    entity2id = {}
    with open(csv_path) as f:
        f_csv = csv.DictReader(f)
        for row in f_csv:
            id = row['obj']
            if id.startswith('NCBI gene:') or id.startswith('Uniprot:') or \
                    id.startswith('gene:') or id.startswith('protein:'):

                # if '|' in id:
                #     id = id.split('|')
                #     temp = []
                #     for item in id:
                #         temp.append(':'.join([item.split(':')[0], item.split(':')[1].lower()]))
                #     id = '|'.join(temp)
                # else:
                #     id = id.split(':')
                #     id = ':'.join([id[0], id[1].lower()])

                if id.startswith('gene:') or id.startswith('protein:'):
                    id = id.lower()
                #  对实体进行过滤
                entity = strippingAlgorithm(row['text'])[0]
                if entity not in entity2id:
                    entity2id[entity] = OrderedDict()
                if id not in entity2id[entity]:
                    entity2id[entity][id] = 1
                else:
                    entity2id[entity][id] += 1
                    # if row['text']=='F4/80':
                    #     print(row['text'])
                    #     print(entity)     # f480
        print('entity2id字典总长度：{}'.format(len(entity2id)))  # 4218

    # 按频度重新排序
    entity2id_1 = {}
    for key, value in entity2id.items():
        value_sorted = sorted(value.items(), key=lambda item: item[1], reverse=True)
        entity2id_1[key] = [item[0] for item in value_sorted]

    # 将protein排在前面
    entity2id_2 = {}
    for key, value in entity2id_1.items():
        protein_list = []
        gene_list = []
        for id in value:
            if id.startswith('Uniprot:') or id.startswith('protein:'):
                protein_list.append(id)
            elif id.startswith('NCBI gene:') or id.startswith('gene:'):
                gene_list.append(id)
            entity2id_2[key] = protein_list + gene_list

    print('F4/80: {}'.format(entity2id_2.get('f4 / 80')))  # ['Uniprot:Q61549']    ['NCBI gene:13733', 'Uniprot:Q61549']
    print('F480: {}'.format(entity2id_2.get('f480')))  # ['Uniprot:Q61549']    ['NCBI gene:13733', 'Uniprot:Q61549']
    return entity2id_2


def search_id_from_Uniprot(query_list, reviewed=True):
    # Uniprot 数据库API查询-reviewed
    for query in query_list:
        if reviewed:
            res_reviewed = u.search(query + '+reviewed:yes', frmt="tab", limit=3)
        else:
            res_reviewed = u.search(query, frmt="tab", limit=3)
        if isinstance(res_reviewed, int):   # 400
            print('{} 请求无效 {}'.format(query, res_reviewed))
        elif res_reviewed:  # 若是有返回结果
            Ids = extract_id_from_res(res_reviewed)
            return ['Uniprot:' + Ids[i] for i in range(len(Ids))]
    return []


def search_id_from_NCBI(query_list):
    # NCBI-gene数据库API查询
    for query in query_list:
        try:
            handle = Entrez.esearch(db="gene", idtype="acc", sort='relevance', term=query+'[Gene]')
            record = Entrez.read(handle)
        except (RuntimeError, OSError, TypeError) as e:
            print(e)
            continue
        except URLError as e:
            print(e)
            continue
        if record["IdList"]:
            return ['NCBI gene:' + record["IdList"][i] for i in
                               range(len(record["IdList"][:3]))]
    return []


def getEntityList(sentence, predLabels):
    '''
    抽取一个句子中的所有实体
    '''
    entity_list = []
    position_list = []
    leixing_list = []
    entity = ''
    prex = 0
    for tokenIdx in range(len(sentence)):
        label = predLabels[tokenIdx]
        word = sentence[tokenIdx]
        if label == 0 or label == 1 or label == 3:
            if entity:
                position = tokenIdx - len(entity.split())
                entity, position = postprocess(entity, sentence, position)
                # 仅保留第一次出现且长度大于1的实体
                if len(entity) > 2 and entity not in entity_list:
                    entity_list.append(entity)
                    position_list.append(position)
                    leixing_list.append('protein' if prex == 1 or prex == 2 else 'gene')
                else:
                    pass
                entity = ''
            if label == 1 or label == 3:
                entity = str(word) + ' '
            prex = label

        elif label == 2:
            if prex == 1 or prex == 2:
                entity += word + ' '
                prex = label
            else:
                # [1 2 2 0 0 0 0 0 2 2 0]
                # print(predLabels)
                print('标签错误?->2，跳过！')  # ?次出现
        else:
            if prex == 3 or prex == 4:
                entity += word + ' '
                prex = label
            else:
                print('标签错误?->4，跳过！')  # ?次出现

    if entity:  # 只存在1个
        print('!!!!!!!!!!!!')
        position = tokenIdx - len(entity.split())
        entity, position = postprocess(entity, sentence, position)
        # 仅保留第一次出现且长度大于1的实体
        if len(entity) > 2 and entity not in entity_list:
            entity_list.append(entity)
            position_list.append(position)
            leixing_list.append('protein' if prex == 1 or prex == 2 else 'gene')
        entity = ''

    # 多个词组成的实体中，单个组成词也可能是实体(F值差别不大)
    # 按实体的长度逆序排序，长度较长的实体优先
    res = sorted(enumerate(entity_list), key=lambda x: len(x[1]), reverse=True)
    entity_list = [x[1] for x in res]
    position_list = [position_list[x[0]] for x in res]
    leixing_list = [leixing_list[x[0]] for x in res]

    # if 'ersus' in entity_list:
    #     print(sentence)

    return entity_list, position_list, leixing_list


def searchEntityId(s, predLabels, entity2id, text_byte):
    '''
    对一个句子中的所有实体进行ID链接：

    先是词典精确匹配
    然后是知识库API匹配
    最后是模糊匹配??
    '''
    entity_list, position_list, leixing_list = getEntityList(s, predLabels)
    assert len(entity_list) == len(position_list) == len(leixing_list)

    id_dict = {}
    for i in range(len(entity_list)):
        leixing = leixing_list[i]
        entity = entity_list[i]

        global all_id
        id_dict[entity] = all_id.pop()

        # # # 修正实体格式(去掉空格/标点右边加空格/标点左右加空格)
        # # entity1 = entity
        # # if entity.encode('utf-8') not in text_byte:
        # #     entity1 = entity.replace(' ', '')
        # #     if entity1.encode('utf-8') not in text_byte:
        # #         entity1 = entity
        # #         for punc in string.punctuation:
        # #             if punc in entity1:
        # #                 entity1 = entity1.replace(punc, punc + ' ')
        # #         entity1 = entity1.replace('  ', ' ')
        # #         if entity1.encode('utf-8') not in text_byte:
        # #             entity1 = entity
        # #             for punc in string.punctuation:
        # #                 if punc in entity1:
        # #                     entity1 = entity1.replace(punc, ' ' + punc + ' ')
        # #             entity1 = entity1.replace('  ', ' ')
        # #             if entity1.encode('utf-8') not in text_byte:
        # #                 entity1 = entity
        # # entity = entity1
        # # entity_list[i] = entity1
        #
        # # 实体拓展
        # v1, v2, v3 = strippingAlgorithm(entity)
        # query_list = [entity, v1, v2, v3]
        #
        # # 词典精确匹配
        # if v1 in entity2id:
        #     Ids = entity2id[v1]
        #     id_dict[entity] = Ids
        #
        #     # 区分类型
        #     if leixing == 'gene':
        #         id_dict[entity] = [Ids[k] for k in range(len(Ids)) if
        #                            Ids[k].startswith('gene') or Ids[k].startswith('NCBI')]
        #     elif leixing == 'protein':
        #         id_dict[entity] = [Ids[k] for k in range(len(Ids)) if
        #                            Ids[k].startswith('protein') or Ids[k].startswith('Uniprot')]
        #
        #     '''词典匹配的结果中可能未包含正确答案，但是再加入API查询的时间开销过大....放弃!'''
        #     all_id.append(id_dict[entity])
        #     continue
        #
        # # 知识库API查询
        # if entity not in id_dict:
        #     if leixing == 'protein':
        #         Ids = search_id_from_Uniprot(query_list, reviewed=True) # Uniprot-reviewed
        #         if Ids:
        #             id_dict[entity] = Ids
        #             entity2id[v1] = Ids
        #         else:
        #             Ids = search_id_from_Uniprot(query_list, reviewed=False) # Uniprot-unreviewed
        #             if Ids:
        #                 id_dict[entity] = Ids
        #                 entity2id[v1] = Ids
        #             else:
        #                 Ids = search_id_from_NCBI(query_list)   # NCBI-gene
        #                 if Ids:
        #                     id_dict[entity] = Ids
        #                     entity2id[v1] = Ids
        #     else:
        #         Ids = search_id_from_NCBI(query_list)
        #         if Ids:
        #             id_dict[entity] = Ids
        #             entity2id[v1] = Ids
        #         else:
        #             Ids = search_id_from_Uniprot(query_list, reviewed=True)
        #             if Ids:
        #                 id_dict[entity] = Ids
        #                 entity2id[v1] = Ids
        #             else:
        #                 Ids = search_id_from_Uniprot(query_list, reviewed=False)
        #                 if Ids:
        #                     id_dict[entity] = Ids
        #                     entity2id[v1] = Ids
        #
        #     # else:
        #     #     # 模糊匹配--计算 Jaro–Winkler 距离
        #     #     max_score = -1
        #     #     max_score_key = ''
        #     #     for key in entity2id.keys():
        #     #         score = Levenshtein.jaro_winkler(key, entity.lower())
        #     #         if score > max_score:
        #     #             max_score = score
        #     #             max_score_key = key
        #     #     Ids = entity2id.get(max_score_key)
        #     #     id_dict[entity] = [Ids[k] for k in range(len(Ids)) if
        #     #                        Ids[k].startswith('gene') or Ids[k].startswith('NCBI')]
        #     #     entity2id[entity_variants1] = id_dict[entity]
        #
        # if entity not in id_dict:
        #     print('未找到{}的ID，空'.format(entity))  # 152次出现
        #     id_dict[entity] = []
        #
        # all_id.append(id_dict[entity])

    return entity_list, id_dict, position_list, leixing_list


def entity_disambiguation_cnn(entity, entity_id, cnn, x_sen, x_data,
                              pos_sen, x_id_dict, position, stop_word, leixing, prob):
    type_id = ''
    context_window_size = 10
    batch_size = 32
    entity_id = entity_id[:batch_size]

    # 若实体的id中存在entity=id的情况，则仅标注其实体类型
    if entity_id.count('protein:' + entity.lower()) >= 1 and leixing=='protein':
        # print('仅标注实体类型-protein')
        type_id = 'protein:' + entity
        return type_id
    elif entity_id.count('gene:' + entity.lower()) > 1 and leixing=='gene':
        # print('仅标注实体类型-gene')
        type_id = 'gene:' + entity
        return type_id

    fea_list = {'x_left': [], 'x_right': [], 'x_left_pos': [], 'x_right_pos': [],
                'x_id': [], 'x_elmo_l': [], 'x_elmo_r': []}

    num = 0
    end_l = position
    elmo_sen_left = []
    x_sen_left = []
    pos_sen_left = []
    while num < context_window_size:
        end_l -= 1
        if end_l > 0:
            # 过滤停用词 stop_word
            if x_sen[end_l] not in stop_word:
                elmo_sen_left.append(x_data[end_l])
                x_sen_left.append(x_sen[end_l])
                pos_sen_left.append(pos_sen[end_l])
                num += 1
        else:
            elmo_sen_left.append("__PAD__")
            x_sen_left.append(0)
            pos_sen_left.append(0)
            num += 1
    x_sen_left = x_sen_left[::-1]
    pos_sen_left = pos_sen_left[::-1]

    num = 0
    start_r = position + len(entity.split())
    elmo_sen_right = []
    x_sen_right = []
    pos_sen_right = []
    while num < context_window_size:
        if start_r < len(x_sen):
            # 过滤停用词 stop_word
            if x_sen[start_r] not in stop_word:
                elmo_sen_right.append(x_data[start_r])
                x_sen_right.append(x_sen[start_r])
                pos_sen_right.append(pos_sen[start_r])
                num += 1
        else:
            elmo_sen_right.append("__PAD__")
            x_sen_right.append(0)
            pos_sen_right.append(0)
            num += 1
        start_r += 1

    assert len(x_sen_left) == len(x_sen_right)

    entity_id_new = []
    for i in range(len(entity_id)):
        id = entity_id[i]
        if id.startswith('gene') or id.startswith('protein'):
            continue
        entity_id_new.append(id)

        if '|' in id:
            id = id.split('|')
            id = '|'.join([item.split(':')[1] for item in id])
        else:
            id = id.split(':')[1]

        if id in x_id_dict:
            x_id = x_id_dict[id]
        else:
            # x_id = 0  # 0.386

            # 丢弃此候选ID×
            # print('{} not in x_id_dict (2000)'.format(id))  # 1345
            entity_id_new.pop()
            continue

        fea_list['x_left'].append(x_sen_left)
        fea_list['x_right'].append(x_sen_right)
        fea_list['x_left_pos'].append(pos_sen_left)
        fea_list['x_right_pos'].append(pos_sen_right)
        fea_list['x_id'].append([x_id])
        fea_list['x_elmo_l'].append(elmo_sen_left)
        fea_list['x_elmo_r'].append(elmo_sen_right)

    if not fea_list['x_id']:
        print('空空如也')
        if leixing == 'protein':
            type_id = 'protein:' + entity
        else:
            type_id = 'gene:' + entity
        return type_id

    # if not fea_list['x_id']:
    #     print('空空如也')
    #     if leixing == 'protein':
    #         return 'protein:' + entity
    #     else:
    #         if entity_id.count('gene:' + entity.lower()) >= 1:
    #             return 'gene:' + entity
    #         else:
    #             return entity_id[0]

    lenth = len(fea_list['x_id'])
    assert lenth == len(entity_id_new)

    while len(fea_list['x_id']) < batch_size:
        fea_list['x_left'].append(fea_list['x_left'][0])
        fea_list['x_right'].append(fea_list['x_right'][0])
        fea_list['x_left_pos'].append(fea_list['x_left_pos'][0])
        fea_list['x_right_pos'].append(fea_list['x_right_pos'][0])
        fea_list['x_id'].append(fea_list['x_id'][0])
        fea_list['x_elmo_l'].append(fea_list['x_elmo_l'][0])
        fea_list['x_elmo_r'].append(fea_list['x_elmo_r'][0])

    for key, value in fea_list.items():
        fea_list[key] = np.array(fea_list[key])

    dataSet = [fea_list['x_id'], fea_list['x_left'], fea_list['x_right']]
    # dataSet = [fea_list['x_id'], fea_list['x_left'], fea_list['x_right'], fea_list['x_elmo_l'], fea_list['x_elmo_r']]


    predictions = cnn.predict(dataSet)
    # assert len(entity_id_new)==len(predictions)
    if entity=='SuFu' or entity=='Smo' or entity=='Ptch1' or entity=='Gli1':
        print(entity)
        print(entity_id)
        print(predictions)

    max_dis = 0.1     # 设定一个阙值(很重要)√
    # max_dis = 0     # 设定一个阙值
    for i in range(len(predictions[:lenth])):
        if i==0:
            predictions[i][1] = predictions[i][1]*prob  # 5 5.5 7 24 float("inf")
        if predictions[i][1] > max_dis:
            max_dis = predictions[i][1]
            type_id = entity_id_new[i]

    return type_id if type_id else entity_id[0]


all_id = []
def writeOutputToFile(path, predLabels, ned_model, prob):
    '''
    按顺序读取原文件夹中的xml格式文件
    同时，对应每个text生成annotation标签：
        getElementsByTagName方法：获取孩子标签
        getAttribute方法：可以获得元素的属性所对应的值。
        firstChild.data≈childNodes[0].data：返回被选节点的第一个子标签对之间的数据
    将实体预测结果写入XML文件

    :param path: 测试数据路径
    :param predLabels: 测试数据的实体预测结果
    :param maxlen: 句子截断长度
    :param split_pos: 划分训练集和验证集的位置
    :return:
    '''
    exit = 0
    not_exit = 0
    not_find = 0
    num_match = 0
    idx_line = -1
    num_entity_no_id = 0
    words_with_multiId = []
    root = '/home/administrator/PycharmProjects/keras_bc6_track1/sample/'
    base = r'/home/administrator/桌面/BC6_Track1'
    BioC_path = base + '/' + 'test_corpus_20170804/caption_bioc'  # 测试数据文件夹
    dic_path = base + '/' + 'BioIDtraining_2/annotations.csv'  # 实体ID查找词典文件
    result_path = base + '/' + 'test_corpus_20170804/prediction'

    # 读取golden实体的ID词典(仅训练集)
    entity2id = getCSVData(dic_path)

    # 获取测试集所有句子list的集合
    sen_list = get_test_out_data(path)

    # 读取测试预料的数据和golden ID
    with open(root + 'data/test.pkl', "rb") as f:
        test_x, test_elmo, test_y, test_char, test_cap, test_pos, test_chunk, test_dict = pkl.load(f)
    with open('/home/administrator/PycharmProjects/embedding/length.pkl', "rb") as f:
        word_maxlen, sentence_maxlen = pkl.load(f)

    sentence_maxlen = 427

    # all_id.txt
    with open(root + 'all_id_elmo.txt', "r") as f:
        for line in f:
            line = line.strip('\n')
            if line:
                all_id.append(line.split('\t'))
            else:
                all_id.append([])
    all_id.reverse()

    # # 加载词向量矩阵
    # embeddingPath = r'/home/administrator/PycharmProjects/embedding'
    # with open(embeddingPath + '/emb.pkl', "rb") as f:
    #     embedding_matrix = pkl.load(f)

    # 停用词表/标点符号
    stop_word = [239, 153, 137, 300, 64, 947, 2309, 570, 10, 69, 238, 175, 852, 7017, 378, 136, 5022, 1116, 5194,
                 14048,
                 28, 217, 4759, 7359, 201, 671, 11, 603, 15, 1735, 2140, 390, 2366, 12, 649, 4, 1279, 3351, 3939,
                 5209, 16, 43,
                 2208, 8, 5702, 4976, 325, 891, 541, 1649, 17, 416, 2707, 108, 381, 678, 249, 5205, 914, 5180, 5,
                 20, 18695,
                 15593, 5597, 730, 1374, 18, 2901, 1440, 237, 150, 44, 10748, 549, 3707, 4325, 27, 331, 522, 10790,
                 297, 1060, 1976,
                 7803, 1150, 1189, 2566, 192, 5577, 703, 666, 315, 488, 89, 1103, 231, 16346, 9655, 6569, 605, 6,
                 294, 3932, 24965,
                 9, 775, 4593, 76, 21733, 140, 229, 16368, 21098, 181, 620, 134, 6032, 268, 2267, 22948, 88, 655,
                 24768, 6870,
                 25, 615, 4421, 99, 3, 375, 483, 7, 2661, 32, 2223, 42, 1612, 595, 22, 37, 432, 8439, 67, 15853,
                 6912, 459,
                 21441, 3811, 1538, 1644, 2834, 1192, 5197, 1734, 78, 647, 247, 491, 16228, 23, 578, 34, 47, 77,
                 1239, 846, 26,
                 24317, 785, 3601, 8504, 29, 9414, 520, 3399, 2035, 6778, 96, 2048, 1, 579, 1135, 173, 4089, 4980,
                 205, 63, 516, 169,
                 8413, 1980, 337, 19, 521, 13, 48, 551, 3927, 59, 10281, 11926, 3915]

    if ned_model:
        print('new!')
        with open(root + 'ned/data/x_id_dict.pkl', 'rb') as f:
            x_id_dict = pkl.load(f)
            print('x_id_dict 的长度：', len(x_id_dict))  # 13114
        cnn = ned_model
    else:
        print('old!')
        with open(root + 'ned/data/x_id_dict_old.pkl', 'rb') as f:
            x_id_dict = pkl.load(f)
            print('x_id_dict 的长度：', len(x_id_dict))  # 13352
        # 加载实体消歧模型
        # cnn = load_model(root + 'ned/ned_model/weights_ned_max.hdf5')
        cnn = load_model(root + 'ned/ned_model/weights_rnn_0.9412647734274352.hdf5')  # 0.822 0.871 0.913 0.9412647734274352 0.9690351025945396
        # with open('/home/administrator/PycharmProjects/keras_bc6_track1/sample/ned/data/svm_model.pkl', 'rb') as f:
        #     svm = pkl.load(f)

    synId2entity = {}
    with open(root + 'ned/data/synId2entity.txt') as f:
        for line in f:
            s1, s2 = line.split('\t')
            entities = s2.replace('\n', '').split('::,')
            synId2entity[s1] = entities

    idx2pos = {}
    with open(root + 'data/pos2idx.txt') as f:
        for line in f:
            pos, idx = line.split('\t')
            idx2pos[idx.strip('\n')] = pos

    # 知识库精确匹配
    protein2id, gene2id = {}, {}
    # with open('/home/administrator/PycharmProjects/keras_bc6_track1/sample/pg2id.pkl', 'rb') as f:
    #     protein2id, gene2id = pkl.load(f)

    files = os.listdir(BioC_path)
    files.sort()
    for j in tqdm(range(len(files))):  # 遍历文件夹
        file = files[j]
        if not os.path.isdir(file):  # 判断是否是文件夹，不是文件夹才打开
            f = BioC_path + "/" + file
            try:
                DOMTree = parse(f)  # 使用minidom解析器打开 XML 文档
                collection = DOMTree.documentElement  # 得到了根元素对象
            except:
                print('异常情况：'.format(f))
                continue

            source = collection.getElementsByTagName("source")[0].childNodes[0].data
            date = collection.getElementsByTagName("date")[0].childNodes[0].data  # 时间
            key = collection.getElementsByTagName("key")[0].childNodes[0].data

            # 一、生成dom对象，根元素名collection
            impl = xml.dom.minidom.getDOMImplementation()
            dom = impl.createDocument(None, 'collection', None)  # 创建DOM文档对象
            root = dom.documentElement  # 创建根元素

            source = makeEasyTag(dom, 'source', source)
            date = makeEasyTag(dom, 'date', datetime.datetime.now().strftime('%Y-%m-%d'))
            key = makeEasyTag(dom, 'key', key)

            # 给根节点添加子节点
            root.appendChild(source)
            root.appendChild(date)
            root.appendChild(key)

            # 在集合中获取所有 document 的内容
            documents = collection.getElementsByTagName("document")
            # 一篇文档中的相同实体理应具有县相同的ID?
            SameIdForSameEnt = {}
            for doc in documents:
                id = doc.getElementsByTagName("id")[0].childNodes[0].data
                sourcedata_document = doc.getElementsByTagName("infon")[0].childNodes[0].data
                doi = doc.getElementsByTagName("infon")[1].childNodes[0].data
                pmc_id = doc.getElementsByTagName("infon")[2].childNodes[0].data
                figure = doc.getElementsByTagName("infon")[3].childNodes[0].data
                sourcedata_figure_dir = doc.getElementsByTagName("infon")[4].childNodes[0].data

                document = dom.createElement('document')
                id_node = makeEasyTag(dom, 'id', str(id))
                s_d_node = makeEasyTag(dom, 'infon', str(sourcedata_document))
                doi_node = makeEasyTag(dom, 'infon', str(doi))
                pmc_id_node = makeEasyTag(dom, 'infon', str(pmc_id))
                figure_node = makeEasyTag(dom, 'infon', str(figure))
                s_f_d_node = makeEasyTag(dom, 'infon', str(sourcedata_figure_dir))
                s_d_node.setAttribute('key', 'sourcedata_document')  # 向元素中加入属性
                doi_node.setAttribute('key', 'doi')  # 向元素中加入属性
                pmc_id_node.setAttribute('key', 'pmc_id')  # 向元素中加入属性
                figure_node.setAttribute('key', 'figure')  # 向元素中加入属性
                s_f_d_node.setAttribute('key', 'sourcedata_figure_dir')  # 向元素中加入属性
                document.appendChild(id_node)
                document.appendChild(s_d_node)
                document.appendChild(doi_node)
                document.appendChild(pmc_id_node)
                document.appendChild(figure_node)
                document.appendChild(s_f_d_node)



                passages = doc.getElementsByTagName("passage")
                for passage in passages:
                    text = passage.getElementsByTagName('text')[0].childNodes[0].data
                    text_byte = text.encode('utf-8')
                    annotations = passage.getElementsByTagName('annotation')
                    entity2golden = {}
                    for annotation in annotations:
                        info = annotation.getElementsByTagName("infon")[0]
                        ID = info.childNodes[0].data
                        txt = annotation.getElementsByTagName("text")[0]
                        entity = txt.childNodes[0].data
                        if ID.startswith('gene') or ID.startswith('protein') or ID.startswith(
                                'Uniprot') or ID.startswith('NCBI'):
                            a = strippingAlgorithm(entity)[0]
                            entity2golden[a] = ID

                    '''每读取一篇passage，在<annotation>结点记录识别实体'''
                    idx_line += 1
                    annotation_list = []
                    sentence = sen_list[idx_line][:sentence_maxlen]  # 单词列表形成的句子
                    prediction = predLabels[idx_line]

                    # 根据预测结果来抽取句子中的所有实体，并进行实体链接
                    result = searchEntityId(sentence, prediction, entity2id, text_byte)
                    entity_list, id_dict, position_list, lx_list = result

                    # 收集用到的实体ID去训练消歧模型(可忽略)
                    for i in range(len(entity_list)):
                        entity = entity_list[i]
                        entity_id = id_dict[entity]
                        for id in entity_id:
                            if id.startswith('gene:') or id.startswith('protein:'):
                                continue
                            if '|' in id:
                                id = id.split('|')
                                id = '|'.join([item.split(':')[1] for item in id])
                            else:
                                id = id.split(':')[1]

                            if id not in synId2entity:
                                synId2entity[id] = []
                            if entity not in synId2entity[id]:
                                synId2entity[id].append(entity)
                            # if id not in x_id_dict:
                            #     x_id_dict[id] = len(x_id_dict) + 1

                    ''' 针对多ID的实体进行实体消岐'''
                    annotation_id = 0
                    offset_list = []    # 存储所有实体的位置，避免重叠现象(如：Gli、Gli1)
                    for i in range(len(entity_list)):

                        entity = entity_list[i]
                        entity_id = id_dict[entity]
                        position = position_list[i]
                        leixing = lx_list[i]
                        type_id = ''

                        # 同一篇文档中的相同实体理分配相同的ID
                        if entity in SameIdForSameEnt:
                            # print('entity in entity2id_one')
                            type_id = SameIdForSameEnt[entity]
                        else:
                            if len(entity_id) > 1:
                                if entity not in words_with_multiId:
                                    words_with_multiId.append(entity)
                                else:
                                    pass
                                # First
                                # type_id = entity_id[0]
                                # if leixing == 'protein' and entity_id.count('protein:' + entity) >= 1:
                                #     print('仅标注实体类型-protein')
                                #     type_id = 'protein:' + entity
                                # elif leixing == 'gene' and entity_id.count('gene:' + entity) >= 1:
                                #     print('仅标注实体类型-gene')
                                #     type_id = 'gene:' + entity
                                # else:
                                #     type_id = entity_id[0]

                                # cos/svm/arnn
                                # type_id = entity_disambiguation(entity_id, id2synvec, zhixin, leixing, entity)
                                # type_id = entity_disambiguation_svm(entity, entity_id, id2synvec, zhixin, svm, leixing)
                                # type_id = entity_disambiguation_svm2(entity, entity_id, svm, test_x[idx_line], s, test_pos[idx_line], x_id_dict, position, stop_word, entity2id_one, leixing)
                                type_id = entity_disambiguation_cnn(entity, entity_id, cnn, test_x[idx_line], sentence, test_pos[idx_line], x_id_dict, position, stop_word, leixing, prob)

                                SameIdForSameEnt[entity] = type_id

                            elif len(entity_id) == 1:  # 说明实体对应了唯一ID
                                type_id = entity_id[0]
                            else:
                                if leixing == 'protein':
                                    type_id = 'protein:' + entity
                                else:
                                    type_id = 'gene:' + entity
                                num_entity_no_id += 1

                        # 统计覆盖率
                        a = strippingAlgorithm(entity)[0]
                        goldenID = entity2golden.get(a)
                        if goldenID:
                            if goldenID in entity_id:
                                exit += 1
                                if goldenID == type_id:
                                    num_match += 1
                            else:
                                not_exit += 1
                        else:
                            not_find += 1  # 实体未找到

                        # 对实体编码
                        if entity.encode('utf-8') in text_byte:
                            entity_byte = entity.encode('utf-8')
                        else:
                            entity1 = entity.replace(' ', '')
                            if entity1.encode('utf-8') in text_byte:
                                entity = entity1
                                entity_byte = entity1.encode('utf-8')
                            else:
                                entity2 = entity.replace(',', ', ').replace('.', '. ')
                                if entity2.encode('utf-8') in text_byte:
                                    entity = entity2
                                    entity_byte = entity2.encode('utf-8')
                                else:
                                    entity3 = entity
                                    for punc in string.punctuation:
                                        if punc in entity3:
                                            entity3 = entity3.replace(punc, ' ' + punc + ' ')
                                    if entity3.encode('utf-8') in text_byte:
                                        entity = entity3.replace('  ', ' ')
                                        entity_byte = entity.encode('utf-8')
                                    else:
                                        entity_byte = entity.encode('utf-8')
                                        print('未在句子中找到{}的offset索引？'.format(entity))

                        '''给句子中所有相同的实体标记上相同的ID'''
                        offset = -1
                        while 1:
                            offset = text_byte.find(entity_byte, offset + 1)  # 二进制编码查找offset
                            offset = int(offset)
                            if not offset == -1:
                                # if offset > 0 and text[offset-1]==' ' \
                                #         and offset < len(text)-len(entity) and text[offset+len(entity)]==' ':
                                if offset in offset_list:
                                    continue
                                offset_list.append(offset)

                                annotation_id += 1
                                annotation = dom.createElement('annotation')
                                annotation.setAttribute('id', str(annotation_id))
                                infon1 = makeEasyTag(dom, 'infon', type_id)
                                infon1.setAttribute('key', 'type')
                                infon2 = makeEasyTag(dom, 'infon', str(annotation_id))
                                infon2.setAttribute('key', 'sourcedata_figure_annot_id')
                                infon3 = makeEasyTag(dom, 'infon', str(annotation_id))
                                infon3.setAttribute('key', 'sourcedata_article_annot_id')
                                location = dom.createElement('location')
                                location.setAttribute('offset', str(offset))
                                location.setAttribute('length', str(len(entity_byte)))
                                text_node = makeEasyTag(dom, 'text', entity)
                                annotation.appendChild(infon1)
                                annotation.appendChild(infon2)
                                annotation.appendChild(infon3)
                                annotation.appendChild(location)
                                annotation.appendChild(text_node)
                                annotation_list.append(annotation)
                            else:
                                break

                    # 最后串到根结点上，形成一棵树
                    passage1 = dom.createElement('passage')
                    offset1 = makeEasyTag(dom, 'offset', '0')
                    text1 = makeEasyTag(dom, 'text', text)
                    passage1.appendChild(offset1)
                    passage1.appendChild(text1)
                    for annotation in annotation_list:
                        passage1.appendChild(annotation)

                    # 给根节点添加子节点
                    document.appendChild(passage1)
                root.appendChild(document)

            '''
            将DOM对象doc写入文件
            每读完一个file后，将结果写入同名的XML文件
            '''
            Indent(dom, dom.documentElement)  # 美化
            outputName = result_path + '/' + file
            f = open(outputName, 'w')
            writer = codecs.lookup('utf-8')[3](f)
            dom.writexml(f, indent='\t', newl='\n', addindent='\t', encoding='utf-8')
            writer.close()
            f.close()

    print('exit:{}, not_exit:{}'.format(exit, not_exit))  # exit:5440, not_exit:3131
    print('num_match:{}, not_find:{}'.format(num_match, not_find))  # num_match:3784, not_find:1744
    print('测试集预测结果写入成功！')
    print('{}个词未找到对应的ID'.format(num_entity_no_id))  # 751
    print('{}个词有歧义'.format(len(words_with_multiId)))  # 1654
    print('完结撒花')


    # with open('all_id_elmo.txt', "w") as f:
    #     for line in all_id:
    #         f.write('\t'.join(line))
    #         f.write('\n')

    # # 收集用到的实体ID去训练消歧模型(可忽略)
    # with open('/home/administrator/PycharmProjects/keras_bc6_track1/sample/ned/data/x_id_dict2.pkl', 'wb') as f:
    #     pkl.dump((x_id_dict), f, -1)

    # # 收集同义词集用于AutoExtend
    # with open('/home/administrator/PycharmProjects/keras_bc6_track1/sample/ned/data/synsets.txt', "w") as f:
    #     for key, value in synId2entity.items():
    #         f.write('{}\t{}'.format(key, '::,'.join(value)))
    #         f.write('\n')

