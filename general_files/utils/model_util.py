import gensim.downloader as api
import numpy as np
import torch
import torch.nn.functional as F
import transformers.modeling_outputs
from nltk import word_tokenize
from nltk.translate.bleu_score import corpus_bleu, sentence_bleu, SmoothingFunction
from nltk.translate.meteor_score import meteor_score
from nltk.util import ngrams
from rouge import Rouge
from sacrebleu.metrics import BLEU, CHRF
from general_files.utils.common_util import (
    print_dict_to_table,
    Result,
    get_logger,
    print_error_info,
)
from statistics import mean
from sklearn.metrics import accuracy_score
import re
import string
from collections import Counter
from bert_score import score
from general_files.utils.others.q_squared.cal_q_squared import calc_scores
from general_files.modules.pipeline import Pipeline
from general_files.models.hf_custom import ModelNet
from evaluate import load

log = get_logger(__name__)


def caller(methods, result, *args, **kwargs):
    result = Result() if result is None else result
    for method in methods:
        result = globals().get(method)(*args, **kwargs)
    return result


def accuracy(logits, targets, padding_idx=None):
    """
    logits: (batch_size, max_len, vocab_size)
    targets: (batch_size, max_len)
    """
    _, preds = logits.max(dim=2)
    trues = (preds == targets).float()
    if padding_idx is not None:
        weights = targets.ne(padding_idx).float()
        acc = (weights * trues).sum(dim=1) / weights.sum(dim=1)
    else:
        acc = trues.mean(dim=1)
    acc = acc.mean()
    return acc


def attn_accuracy(logits, targets):
    """
    logits: (batch_size, vocab_size)
    targets: (batch_size)
    """
    _, preds = logits.squeeze(1).max(dim=-1)
    trues = (preds == targets).float()
    acc = trues.mean()
    return acc


def get_ppl_by_labels(logits, targets, weight=None, padding_idx=None):
    """
    logits: (batch_size, max_len, vocab_size)
    targets: (batch_size, max_len)
    """
    batch_size = logits.size(0)
    if weight is None and padding_idx is not None:
        weight = torch.ones(logits.size(-1))
        weight[padding_idx] = 0
    nll = F.nll_loss(
        input=logits.view(-1, logits.size(-1)),
        target=targets.contiguous().view(-1),
        weight=weight,
        reduction="none",
    )
    nll = nll.view(batch_size, -1).sum(dim=1)
    if padding_idx is not None:
        word_cnt = targets.ne(padding_idx).float().sum()
        nll = nll / word_cnt
    ppl = nll.exp()
    return ppl


def get_padding_mask(seq, pad_token_id):
    """获取句子的mask矩阵以及长度

    Args:
        seq: tensor[bsz, seq_len]
        pad_token_id: int, 句子填充的pad所对应的id

    Returns:
        mask: desc
        seq_lens:

    """
    ones = torch.ones_like(seq)
    zeros = torch.zeros_like(seq)
    mask = torch.where(seq == pad_token_id, zeros, ones)
    seq_lens = mask.sum(dim=1)
    return mask, seq_lens


def get_ppl(df, model, tokenizer):
    loss_dict = df.map(
        lambda batch: {"loss": compute_loss(batch, model, tokenizer)},
        batched=True,
        batch_size=1,
        desc="Compute PPL",
    )

    eval_loss = []
    for item in list(loss_dict):
        eval_loss.append(item["loss"])
    ppl = np.mean(eval_loss)
    return ppl


def get_bert_score(df, config):
    # scorer = BertScorer()
    # scorer.init_scorer(lang='en', num_layers=8, rescale_with_baseline=True)
    generated = df["generated_seqs"]
    target = df["bert_score_reference"]
    # scores = scorer.get_score(target, generated)[-1]
    scores = score(
        generated,
        target,
        lang="en",
        verbose=False,
        rescale_with_baseline=True,
        device=config.default_device,
    )[-1].numpy()
    return round(mean(list(scores)), 4)


def get_q_squared_score(df, config=None):
    # scorer = BertScorer()
    # scorer.init_scorer(lang='en', num_layers=8, rescale_with_baseline=True)
    generated = df["generated_seqs"]
    knowledge = df["knowledge"]
    # scores = scorer.get_score(target, generated)[-1]
    q_2_nli, q_2_f1 = calc_scores(generated, knowledge, config=config)
    return round((q_2_nli), 4), round((q_2_f1), 4)




def compute_loss(batch, model, tokenizer):
    input_ids, labels = torch.LongTensor(batch["input_ids"]).to(
        model.device
    ), torch.LongTensor(batch["labels"]).to(model.device)
    ignore_columns = [
        "source",
        "target",
        "other_features",
        "generated",
        "input_ids",
        "labels",
        "decoder_input",
        "decoder_prompt",
        "decoder_input_ids",
    ]
    other_features = dict()
    input_ids_len = input_ids.ne(tokenizer.pad_token_id).int().sum(-1).unsqueeze(1)
    other_features["input_ids_len"] = input_ids_len
    for k in batch.data.keys():
        if k not in ignore_columns and "decoder_" in k:
            try:
                other_features[k] = torch.LongTensor(batch[k]).to(model.device)
            except ValueError:
                other_features[k] = batch[k]
    with torch.no_grad():
        model.stage = "train"
        outputs = model(input_ids=input_ids, labels=labels, **other_features)
        eval_loss = outputs.lm_loss

    return [eval_loss.exp().item()]


def clean_text(text):
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\b(a|an|the|in|our)\b", " ", text)
    return re.sub(" +", " ", text).strip()


def compute_f1(test_df):
    """
    This function is copied from: https://github.com/orhonovich/q-squared/blob/main/pipeline/score.py
    """
    candidates = test_df["generated_seqs"]
    references = test_df["f1_reference"]
    f1_list = []
    for i, a_gold in enumerate(references):
        a_pred = candidates[i]
        if a_pred == "":
            f1_list.append(0)
            continue
        gold_toks = clean_text(a_gold).split()
        pred_toks = clean_text(a_pred).split()
        common = Counter(gold_toks) & Counter(pred_toks)
        num_same = sum(common.values())
        if num_same == 0:
            f1_list.append(0)
            continue
        precision = 1.0 * num_same / len(pred_toks)
        recall = 1.0 * num_same / len(gold_toks)
        f1 = (2 * precision * recall) / (precision + recall)
        f1_list.append(f1)
    return round(sum(f1_list) / len(f1_list) * 100, 2)


def compute_chrf(references, candidates):
    chrf = CHRF(word_order=2)
    return round(chrf.corpus_score(candidates, [references]).score, 4)


def compute_sacre_bleu(references, candidates):
    bleu = BLEU()
    return round(bleu.corpus_score(candidates, [references]).score, 4)


def compute_sent_bleu(references, candidates):
    bleu1 = 0.0
    bleu2 = 0.0
    bleu3 = 0.0
    bleu4 = 0.0
    ref_list, dec_list = [], []
    for i in range(len(candidates)):
        dec_list.append(word_tokenize(candidates[i]))
        if type(references[i]) is list:
            tmp = []
            for ref in references[i]:
                tmp.append(word_tokenize(ref))
            ref_list.append(tmp)
        else:
            ref_list.append([word_tokenize(references[i])])

    for example_id, (label, pred) in enumerate(zip(ref_list, dec_list)):
        bleu1 += sentence_bleu(
            label,
            pred,
            weights=[1, 0, 0, 0],
            smoothing_function=SmoothingFunction().method3,
        )
        bleu2 += sentence_bleu(
            label,
            pred,
            weights=[0.5, 0.5, 0, 0],
            smoothing_function=SmoothingFunction().method3,
        )
        bleu3 += sentence_bleu(
            label,
            pred,
            weights=[1 / 3, 1 / 3, 1 / 3, 0],
            smoothing_function=SmoothingFunction().method3,
        )
        bleu4 += sentence_bleu(
            label,
            pred,
            weights=[0.25, 0.25, 0.25, 0.25],
            smoothing_function=SmoothingFunction().method3,
        )
    bleu1 = bleu1 / len(ref_list)
    bleu2 = bleu2 / len(ref_list)
    bleu3 = bleu3 / len(ref_list)
    bleu4 = bleu4 / len(ref_list)
    return (
        round(bleu1 * 100, 4),
        round(bleu2 * 100, 4),
        round(bleu3 * 100, 4),
        round(bleu4 * 100, 4),
    )


def compute_corpus_bleu(references, candidates):
    bleu1 = 0.0
    bleu2 = 0.0
    bleu3 = 0.0
    bleu4 = 0.0
    ref_list, dec_list = [], []
    for i in range(len(candidates)):
        dec_list.append(word_tokenize(candidates[i]))
        if type(references[i]) is list:
            tmp = []
            for ref in references[i]:
                tmp.append(word_tokenize(ref))
            ref_list.append(tmp)
        else:
            ref_list.append([word_tokenize(references[i])])
    bleu1 = corpus_bleu(ref_list, dec_list, weights=(1, 0, 0, 0))
    bleu2 = corpus_bleu(ref_list, dec_list, weights=(0, 1, 0, 0))
    bleu3 = corpus_bleu(ref_list, dec_list, weights=(0, 0, 1, 0))
    bleu4 = corpus_bleu(ref_list, dec_list, weights=(0, 0, 0, 1))
    return (
        round(bleu1 * 100, 4),
        round(bleu2 * 100, 4),
        round(bleu3 * 100, 4),
        round(bleu4 * 100, 4),
    )


def compute_meteor(references, candidates):
    score_list = []
    for i in range(len(candidates)):
        if type(references[i]) is list:
            ref_list = references[i]
        else:
            ref_list = [references[i]]
        ref = [r.split(" ") for r in ref_list]
        cand = candidates[i].split(" ")
        score = meteor_score(ref, cand)
        score_list.append(score)
    return round(np.mean(score_list), 4)


def compute_rouge(references, candidates):
    rouge = Rouge()
    scores = rouge.get_scores(candidates, references)
    rouge_1 = [score["rouge-1"]["f"] * 100 for score in scores]
    rouge_2 = [score["rouge-2"]["f"] * 100 for score in scores]
    rouge_l = [score["rouge-l"]["f"] * 100 for score in scores]
    return (
        round(np.mean(rouge_1), 4),
        round(np.mean(rouge_2), 4),
        round(np.mean(rouge_l), 4),
    )


def distinct_ngram(candidates, n=2):
    """Return basic ngram statistics, as well as a dict of all ngrams and their freqsuencies."""
    ngram_freqs = {}  # ngrams with frequencies
    ngram_len = 0  # total number of ngrams
    for candidate in candidates:
        for ngram in ngrams(word_tokenize(candidate), n):
            ngram_freqs[ngram] = ngram_freqs.get(ngram, 0) + 1
            ngram_len += 1
    # number of unique ngrams
    uniq_ngrams = len([val for val in ngram_freqs.values() if val == 1])
    distinct_ngram = len(ngram_freqs) / ngram_len if ngram_len > 0 else 0
    return round(distinct_ngram, 4)


def knowledge_f1(references, candidates, work_dir):
    """
    This function is copied from: https://github.com/PaddlePaddle/Research/blob/master/NLP/Dialogue-PLATO/tools/knowledge_f1.py
    """
    cnt = 0
    res = 0.0
    r = 0.0
    p = 0.0
    stopwords = set()
    with open(f"{work_dir}/data/stopwords.txt") as f:
        for line in f:
            word = line.strip()
            stopwords.add(word)

    for candidate, reference in zip(candidates, references):
        cnt += 1
        if type(reference) is list:
            reference = reference[0]
        knowledges = reference.strip().split("\t")

        words = set()
        for sent in knowledges:
            for word in sent.split():
                words.add(word.lower())
        words = words - stopwords
        k_len = len(words)

        pred = set()
        for word in candidate.split():
            pred.add(word.lower())
        pred = pred - stopwords
        pred_len = len(pred)
        overlap = len(words & pred)

        if overlap == 0:
            continue

        recall = float(overlap) / k_len
        r += recall
        precison = float(overlap) / pred_len
        p += precison
        res += 2 * recall * precison / (recall + precison)
    recall = round(r / cnt, 4)
    precision = round(p / cnt, 4)
    f1 = round(res / cnt, 4)
    return recall, precision, f1


def compute_cos_sim(references, candidates, work_dir):
    # load pre-trained word-vectors from gensim-data
    word_vectors = api.load("glove-wiki-gigaword-100")
    vocab_list = word_vectors.index_to_key

    stopwords = set()
    with open(f"{work_dir}/data/stopwords.txt") as f:
        for line in f:
            word = line.strip()
            stopwords.add(word)

    sim_list = []
    for i in range(len(candidates)):
        dec_set = set()
        for word in word_tokenize(candidates[i]):
            word = word.lower()
            if word in vocab_list:
                dec_set.add(word)
        dec_set = dec_set - stopwords
        dec_list = list(dec_set)
        if len(dec_list) == 0:
            continue

        ref_set = set()
        for word in word_tokenize(references[i]):
            word = word.lower()
            if word in vocab_list:
                ref_set.add(word)
        ref_set = ref_set - stopwords
        ref_list = list(ref_set)
        # compute cosine similarity between two sets of docvecs from the trained set
        cos_sim = word_vectors.n_similarity(dec_list, ref_list)
        sim_list.append(cos_sim)

    avg_sim = np.mean(sim_list)
    cos_similarity = round(avg_sim, 4)
    return cos_similarity


def compute_cls_acc(references, candidates):
    return accuracy_score(references, candidates)


def get_eval_metrics(test_df, config, tokenizer):
    """
    评价指标计算
    :param config:
    :param test_df: Dataframe类型,必须要包含的column为 [generated, reference, other_features, input_ids, labels]
    :return: dict
    """
    test_result = Result()
    eval_metrics = config.eval_metrics
    if "generated_seqs" in test_df.column_names:
        generated_seqs = test_df["generated_seqs"]
    else:
        generated_seqs = test_df["generated"]
    if "reference" in test_df.column_names:
        reference = test_df["reference"]
    else:
        return test_result

    ###############################################
    # 计算 nlg_eval
    ###############################################
    if "nlg_eval" in eval_metrics:
        from nlgeval import NLGEval

        log.info("计算 nlg_eval ing...")
        try:
            nlgeval = NLGEval()  # loads the models
            metrics_dict = nlgeval.compute_metrics([reference], generated_seqs)
            for key, value in metrics_dict.items():
                metrics_dict[key] = round(value, 4)
            log.info(f"nlg_eval = {str(metrics_dict)}")
            test_result.merge_or_update(metrics_dict)
        except Exception as e:
            print_error_info(e)
            log.error("计算 nlg_eval 失败")

    ###############################################
    # 计算 PPL
    ###############################################
    if "ppl" in eval_metrics:
        log.info("计算 PPL ing...")
        perplexity = load("perplexity", module_type="metric")
        try:
            ppl = perplexity.compute(predictions=generated_seqs, model_id="gpt2")[
                "mean_perplexity"
            ]
        except Exception as e:
            log.error("计算 PPL 失败")
            print_error_info(e)
            ppl = 9999
        # ppl = get_ppl(test_df, model, tokenizer)
        ppl = round(ppl, 4)
        test_result.add(ppl=ppl)
        log.info(f"PPL = {str(ppl)}")

    ###############################################
    # 计算 f1
    ###############################################
    if "f1" in eval_metrics:
        log.info("计算 F1 ing...")
        f1 = compute_f1(test_df)
        test_result.add(f1=f1)
        log.info(f"f1 = {str(f1)}")

    ###############################################
    # 计算 google_bleu
    ###############################################
    if "google_bleu" in eval_metrics:
        log.info("计算 google_bleu ing...")
        google_bleu = load("google_bleu")
        try:
            google_bleu_score = google_bleu.compute(
                predictions=generated_seqs, references=reference
            )["google_bleu"]
            google_bleu_score = google_bleu_score * 100
        except Exception as e:
            log.error("计算 google_bleu 失败")
            print_error_info(e)
            google_bleu_score = 9999
        google_bleu_score = round(google_bleu_score, 4)
        test_result.add(google_bleu=google_bleu_score)
        log.info(f"google_bleu = {str(google_bleu_score)}")

    ###############################################
    # 计算 sacrebleu
    ###############################################
    if "sacrebleu" in eval_metrics:
        log.info("计算 Sacre BLEU ing...")
        bleu = compute_sacre_bleu(reference, generated_seqs)
        test_result.add(sacrebleu=bleu)
        log.info(f"sacrebleu = {str(bleu)}")

    ###############################################
    # 计算 sent_bleu
    ###############################################
    if "sent_bleu" in eval_metrics:
        bleu1, bleu2, bleu3, bleu4 = compute_sent_bleu(reference, generated_seqs)
        test_result.add(
            sent_bleu1=bleu1,
            sent_bleu2=bleu2,
            sent_bleu3=bleu3,
            sent_bleu4=bleu4,
        )
        log.info(f"sent_bleu1 = {str(bleu1)}")
        log.info(f"sent_bleu2 = {str(bleu2)}")
        log.info(f"sent_bleu3 = {str(bleu3)}")
        log.info(f"sent_bleu4 = {str(bleu4)}")

    ###############################################
    # 计算 corpus_bleu
    ###############################################
    if "corpus_bleu" in eval_metrics:
        bleu1, bleu2, bleu3, bleu4 = compute_corpus_bleu(reference, generated_seqs)
        test_result.add(
            corpus_bleu1=bleu1,
            corpus_bleu2=bleu2,
            corpus_bleu3=bleu3,
            corpus_bleu4=bleu4,
        )
        log.info(f"corpus_bleu1 = {str(bleu1)}")
        log.info(f"corpus_bleu2 = {str(bleu2)}")
        log.info(f"corpus_bleu3 = {str(bleu3)}")
        log.info(f"corpus_bleu4 = {str(bleu4)}")

    ###############################################
    # 计算 Dist
    ###############################################
    if "dist" in eval_metrics:
        log.info("计算 Dist ing...")
        dist1 = distinct_ngram(generated_seqs, n=1)
        dist2 = distinct_ngram(generated_seqs, n=2)
        test_result.add(
            dist1=dist1,
            dist2=dist2,
        )
        log.info(f"dist1 = {str(dist1)}")
        log.info(f"dist2 = {str(dist2)}")

    ###############################################
    # 计算 Meteor
    ###############################################
    if "meteor" in eval_metrics:
        log.info("计算 Meteor ing...")
        meteor = load('meteor')
        meteor_score = meteor.compute(predictions=generated_seqs, references=reference)['meteor']
        meteor_score = round(meteor_score, 4)
        # meteor_score = compute_meteor(reference, generated_seqs)
        test_result.add(meteor=meteor_score)
        log.info(f"meteor = {str(meteor_score)}")

    ###############################################
    # 计算 CharF
    ###############################################
    if "charf" in eval_metrics:
        log.info("计算 chrF ing...")
        charf = compute_chrf(reference, generated_seqs)
        test_result.add(charf=charf)
        log.info(f"charf = {str(charf)}")

    ###############################################
    # 计算 ROUGE
    ###############################################
    if "rouge" in eval_metrics:
        log.info("计算 ROUGE ing...")
        try:
            rouge = load('rouge')
            
            rouge_results = rouge.compute(predictions=generated_seqs,
                        references=reference)
            # rouge_1, rouge_2, rouge_l = compute_rouge(reference, generated_seqs)
            # test_result.add(rouge_1=rouge_1)
            # test_result.add(rouge_2=rouge_2)
            # test_result.add(rouge_l=rouge_l)
            rouge_1 = round(rouge_results['rouge1'], 4)
            rouge_2 = round(rouge_results['rouge2'], 4)
            rouge_L = round(rouge_results['rougeL'], 4)
            rouge_Lsum = round(rouge_results['rougeLsum'], 4)
            test_result.add(rouge_1=rouge_1)
            test_result.add(rouge_2=rouge_2)
            test_result.add(rouge_L=rouge_L)
            test_result.add(rouge_Lsum=rouge_Lsum)
            log.info(f"rouge_1 = {str(rouge_1)}")
            log.info(f"rouge_2 = {str(rouge_2)}")
            log.info(f"rouge_L = {str(rouge_L)}")
            log.info(f"rouge_Lsum = {str(rouge_Lsum)}")
            
        except Exception as e:
            print_error_info(e)
            log.info("Rouge 无法计算，Reference可能为空，请检查生成数据！")

    ###############################################
    # 计算 Bert Score
    ###############################################
    if "bert_score" in eval_metrics:
        log.info("计算 Bert score ing...")
        
        try:
            # scores = get_bert_score(test_df, config)
            # test_result.add(bert_score=scores)
            bertscore = load("bertscore")
            bert_score_reference = test_df["bert_score_reference"]           
            bert_score = mean(bertscore.compute(predictions=generated_seqs, references=bert_score_reference, lang="en", rescale_with_baseline=True)['f1'])
            bert_score = round(bert_score, 4)
            test_result.add(bert_score=bert_score)
            log.info(f"bert_score = {str(bert_score)}")
        except ValueError:
            log.info("bert_score 无法计算，Reference可能为空，请检查生成数据！")

    ###############################################
    # 计算 q_squared
    ###############################################
    if "q_squared" in eval_metrics:
        log.info("计算 q_squared ing...")
        
        try:
            q_squared_nli, q_squared_f1 = get_q_squared_score(test_df, config=config)
            test_result.add(q_squared_nli=q_squared_nli)
            test_result.add(q_squared_f1=q_squared_f1)
            log.info(f"q_squared_nli = {str(q_squared_nli)}")
            log.info(f"q_squared_f1 = {str(q_squared_f1)}")
        except Exception as e:
            print_error_info(e)
            log.info("q_squared 无法计算，请检查数据！")

    ###############################################
    # 计算 分类 Accuracy
    ###############################################
    if "cls_acc" in eval_metrics:
        log.info("计算 分类 Accuracy ing...")
        try:
            accuracy_metric = load("accuracy")
            cls_acc = accuracy_metric.compute(
                references=reference, predictions=generated_seqs
            )["accuracy"]
        except Exception as e:
            print_error_info(e)
            accuracy_metric = 0
        # cls_acc = compute_cls_acc(reference, generated_seqs)
        test_result.add(cls_acc=cls_acc)

    print_dict_to_table(
        test_result,
        "Metric",
        "Score:yum:",
        f"[bold green]Test results of {config.model_processor} on {config.dataset_processor}",
        config,
    )
    return test_result


def pack_result_to_seq2seq_lmoutput(result):
    default_result = Result(
        loss=None,
        logits=None,
        decoder_hidden_states=None,
        decoder_attentions=None,
        cross_attentions=None,
        encoder_last_hidden_state=None,
        encoder_hidden_states=None,
        encoder_attentions=None,
    )
    result.merge(default_result)
    return transformers.modeling_outputs.Seq2SeqLMOutput(
        loss=result.loss,
        logits=result.logits,
        decoder_hidden_states=result.decoder_hidden_states,
        decoder_attentions=result.decoder_attentions,
        cross_attentions=result.cross_attentions,
        encoder_last_hidden_state=result.encoder_last_hidden_state,
        encoder_hidden_states=result.encoder_hidden_states,
        encoder_attentions=result.encoder_attentions,
    )


def nucleus_generate(
    input_ids: torch.Tensor,
    decoder_input_ids: torch.Tensor,
    decoder_eos_token_id: int,
    max_length: int = 100,
    temperature: float = 1.0,
    top_k: int = 0,
    top_p: float = 0.7,
    model=None,
    **other_features,
):
    generated_ids = None
    past_result = None
    for i in range(max_length):
        past_result = model(input_ids, decoder_input_ids, past_result, **other_features)
        logits = past_result["logits"][:, -1, :]
        logits = logits / temperature

        filtered_logits = top_k_top_p_filtering(logits, top_k=top_k, top_p=top_p)
        probabilities = F.softmax(filtered_logits, dim=-1)

        next_token = torch.multinomial(probabilities, 1)
        pred_index = next_token.detach().cpu()

        if (pred_index == decoder_eos_token_id).int().sum() == len(pred_index):
            break
        generated_ids = (
            torch.cat([generated_ids, pred_index], dim=-1)
            if generated_ids is not None
            else pred_index
        )
        if decoder_input_ids is not None:
            decoder_input_ids = torch.cat(
                [decoder_input_ids, pred_index.to(decoder_input_ids.device)], dim=-1
            )
        else:
            input_ids = torch.cat([input_ids, pred_index.to(input_ids.device)], dim=-1)
    for i, s in enumerate(generated_ids):
        ss = s.tolist()
        if decoder_eos_token_id in ss:
            generated_ids[i][ss.index(decoder_eos_token_id) :] = decoder_eos_token_id
    return generated_ids.tolist()


def greedy_generate(
    input_ids: torch.Tensor,
    decoder_input_ids: torch.Tensor,
    decoder_eos_token_id: int,
    max_length: int = 100,
    model=None,
    **other_features,
):
    generated_ids = None
    past_result = None
    softmax = torch.nn.Softmax(dim=-1)
    for _ in range(max_length):
        past_result = model(input_ids, decoder_input_ids, past_result, **other_features)
        probabilities = softmax(past_result["logits"][:, -1, :])
        next_token = torch.multinomial(probabilities, 1)
        pred_index = next_token.detach().cpu()
        if (pred_index == decoder_eos_token_id).int().sum() == len(pred_index):
            break
        generated_ids = (
            torch.cat([generated_ids, pred_index], dim=-1)
            if generated_ids is not None
            else pred_index
        )
        if decoder_input_ids is not None:
            decoder_input_ids = torch.cat(
                [decoder_input_ids, pred_index.to(decoder_input_ids.device)], dim=-1
            )
        else:
            input_ids = torch.cat([input_ids, pred_index.to(input_ids.device)], dim=-1)
    for i, s in enumerate(generated_ids):
        ss = s.tolist()
        if decoder_eos_token_id in ss:
            generated_ids[i][ss.index(decoder_eos_token_id) :] = decoder_eos_token_id
    return generated_ids.tolist()


def top_k_top_p_filtering(
    batch_logits, top_k=10, top_p=0.9, filter_value=-10000.0
) -> torch.tensor:
    assert batch_logits.dim() == 2
    for index in range(batch_logits.shape[0]):
        logits = batch_logits[index]
        top_k = min(top_k, logits.size(-1))
        if top_k > 0:
            indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
            logits[indices_to_remove] = filter_value

        if top_p > 0.0:
            sorted_logits, sorted_indices = torch.sort(logits, descending=True)
            cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

            sorted_indices_to_remove = cumulative_probs > top_p
            sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[
                ..., :-1
            ].clone()
            sorted_indices_to_remove[..., 0] = 0

            indices_to_remove = sorted_indices[sorted_indices_to_remove]
            logits[indices_to_remove] = filter_value
            batch_logits[index] = logits
    return batch_logits


def generate_sentences(model, batch, tokenizer, config):
    input_ids = torch.LongTensor(batch["input_ids"]).to(model.device)
    other_features = model.prepare_other_features_for_generation(batch)
    if config.data_mode == "unilm":
        max_len = config.max_generation_length + len(input_ids[0])
        min_len = config.min_generation_length + len(input_ids[0])
    else:
        max_len = config.max_generation_length
        min_len = config.min_generation_length
    if config.generate_method != "oracle":
        ###############################################
        # 不使用HuggingFace的生成方法，而是自己实现
        ###############################################
        if config.generate_method == "nucleus":
            # Generate with nucleus search.
            generated_ids = nucleus_generate(
                input_ids=input_ids,
                decoder_eos_token_id=tokenizer.eos_token_id,
                top_k=config.top_k,
                top_p=config.top_p,
                temperature=config.temperature,
                max_length=max_len,
                model=model,
                **other_features,
            )
        elif config.generate_method == "greedy":
            # Generate with greedy search.
            generated_ids = greedy_generate(
                input_ids=input_ids,
                decoder_eos_token_id=tokenizer.eos_token_id,
                max_length=max_len,
                model=model,
                **other_features,
            )
    else:
        ###############################################
        # 使用HuggingFace自带的生成方法
        ###############################################
        generated_ids = None
        if hasattr(model, "backbone"):
            other_features["decoder_stage"] = "test"
            ori_generated_ids = model.backbone.generate(
                input_ids=input_ids,
                num_beams=config.beam_size,
                bos_token_id=tokenizer.bos_token_id,
                eos_token_id=tokenizer.eos_token_id,
                do_sample=True,
                top_k=config.top_k,
                top_p=config.top_p,
                max_length=max_len,
                min_length=min_len,
                use_cache=True,
                repetition_penalty=0.9,
                early_stopping=True,
                **other_features,
            )
        else:
            log.info("当前模型不支持自带生成方法，没有backbone，自动切换至nucleus生成！")
            # Generate with nucleus search.
            generated_ids = nucleus_generate(
                input_ids=input_ids,
                decoder_eos_token_id=tokenizer.eos_token_id,
                top_k=config.top_k,
                top_p=config.top_p,
                temperature=config.temperature,
                max_length=max_len,
                model=model,
                **other_features,
            )
    # 只取后边的id
    if not generated_ids:
        if config.data_mode == "unilm":
            generated_ids = [
                g[len(input_ids[i]) :] for i, g in enumerate(ori_generated_ids)
            ]
        else:
            generated_ids = ori_generated_ids
    if config.data_mode == "classification":
        return generated_ids
    generated_sentences = [
        {
            "seqs": tokenizer.decode(sent, skip_special_tokens=True),
            "seqs_with_special_tokens": tokenizer.decode(
                sent, skip_special_tokens=False, ignore_tokens=[tokenizer.pad_token]
            ),
        }
        for sent in generated_ids
    ]
    return generated_sentences


def predict_labels(model, batch, tokenizer, config):
    model.eval()
    input_ids = torch.LongTensor(batch["input_ids"]).to(model.device)
    other_features = model.prepare_other_features_for_generation(batch)
    for key in other_features.keys():
        other_features[key] = other_features[key].to(model.device)
    generated_ids = model(input_ids=input_ids, **other_features)["predict_labels"]
    return generated_ids.cpu().tolist()
