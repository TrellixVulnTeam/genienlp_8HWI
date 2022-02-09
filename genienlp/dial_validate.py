#
# Copyright (c) 2021, The Board of Trustees of the Leland Stanford Junior University
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import copy
import logging
import sys
import time
from collections import defaultdict

import torch
import ujson
from BiToD.evaluate import r_en_API_MAP
from BiToD.knowledgebase import api
from BiToD.knowledgebase.en_zh_mappings import api_names, required_slots
from BiToD.utils import knowledge2span, span2state, state2constraints, state2span
from termcolor import colored

from genienlp.data_utils.example import NumericalizedExamples, SequentialField

logger = logging.getLogger(__name__)

INIT_SYS_MESSAGE = {
    'en': 'Hello! How can I help you today?',
    'fa': 'سلام! امروز چطور می توانم به شما کمک کنم؟',
    'zh': '你好！ 我今天能帮到你什么？',
}


def numericalize_example(input_text, numericalizer, turn_id, device):
    if isinstance(input_text, str):
        input_text = [input_text]
    tokenized_contexts = numericalizer.encode_batch(input_text, field_name='context', features=None)[0]

    numericalized_turn = NumericalizedExamples(
        example_id=[str(turn_id)],
        context=SequentialField(
            value=torch.tensor([tokenized_contexts.value], device=device),
            length=torch.tensor([tokenized_contexts.length], device=device),
            limited=torch.tensor([tokenized_contexts.limited], device=device),
            feature=None,
        ),
        answer=SequentialField(value=None, length=None, limited=None, feature=None),
    )

    return numericalized_turn


def generate(model, args, numericalized_turn, hyperparameter_idx):
    return model.generate(
        numericalized_turn,
        max_output_length=args.max_output_length,
        min_output_length=args.min_output_length,
        num_outputs=args.num_outputs[hyperparameter_idx],
        temperature=args.temperature[hyperparameter_idx] if args.temperature[hyperparameter_idx] > 0 else 1.0,
        repetition_penalty=args.repetition_penalty[hyperparameter_idx],
        top_k=args.top_k[hyperparameter_idx],
        top_p=args.top_p[hyperparameter_idx],
        num_beams=args.num_beams[hyperparameter_idx],
        num_beam_groups=args.num_beam_groups[hyperparameter_idx],
        diversity_penalty=args.diversity_penalty[hyperparameter_idx],
        no_repeat_ngram_size=args.no_repeat_ngram_size[hyperparameter_idx],
        do_sample=args.temperature[hyperparameter_idx] != 0,
    )


def generate_with_seq2seq_model_for_dialogue_interactive(e2e_model, nlg_model, e2e_task, nlg_task):

    bitod_preds = dict()

    predictions = []

    e2e_numericalizer = e2e_model.numericalizer
    e2e_args = e2e_model.args
    device = e2e_model.device

    tgt_lang = e2e_model.tgt_lang

    if e2e_args.nlg_type == 'neural':
        nlg_numericalizer = nlg_model.numericalizer
        nlg_args = nlg_model.args

    dial_id = 'none'
    turn_id = 1
    dialogue_state = {}
    new_state_text = 'null'
    new_knowledge_text = 'null'
    active_api = None
    bitod_preds[dial_id] = {"turns": defaultdict(dict), "API": defaultdict(dict)}

    convo_history = []
    nlg_responses = []
    convo_window = 3

    hyperparameter_idx = 0

    train_target = 'response'

    next_target = {'dst': 'api', 'api': 'response', 'response': 'dst'}

    while True:
        try:
            batch_prediction = []

            # becomes dst for first turn
            train_target = next_target[train_target]

            if train_target == 'dst':
                if convo_history:
                    print(colored(f'SYSTEM: {nlg_responses[-1]}', 'red', attrs=['bold']))
                else:
                    tgt_lang = tgt_lang[:2]
                    print(colored(f'SYSTEM: {INIT_SYS_MESSAGE[tgt_lang]}', 'red', attrs=['bold']))

                # construct new input
                raw_user_input = input(colored('USER: ', 'green', attrs=['bold']))
                if raw_user_input == 'RESET':
                    generate_with_seq2seq_model_for_dialogue_interactive(e2e_model, nlg_model, e2e_task, nlg_task)
                    break
                elif raw_user_input == 'END':
                    sys.exit(0)
                elif raw_user_input == 'STATE':
                    print(f'dialogue state: {dialogue_state}')
                    continue

                raw_user_input = 'USER: ' + raw_user_input.strip()

                convo_history.append(raw_user_input)

                input_text = f'DST: <state> {new_state_text} <history> {" ".join(convo_history[-convo_window:])}'

            elif train_target == 'api':
                new_state_text = state2span(dialogue_state, required_slots)

                # replace state
                input_text = f'API: <state> {new_state_text} <history> {" ".join(convo_history[-convo_window:])}'

            elif train_target == 'response':

                input_text = f'Response: <knowledge> {new_knowledge_text} <state> {new_state_text} <history> {" ".join(convo_history[-convo_window:])}'

            else:
                raise ValueError(f'Invalid train_target: {train_target}')

            numericalized_turn = numericalize_example(input_text, e2e_numericalizer, turn_id, device)
            generated = generate(e2e_model, e2e_args, numericalized_turn, hyperparameter_idx)

            partial_batch_prediction_ids = generated.sequences

            partial_batch_prediction = e2e_numericalizer.reverse(partial_batch_prediction_ids, 'answer')[0]

            # post-process predictions
            partial_batch_prediction = e2e_task.postprocess_prediction(turn_id, partial_batch_prediction)

            # put them into the right array
            batch_prediction.append([partial_batch_prediction])

            predictions += batch_prediction

            if train_target == 'dst':
                # update dialogue_state
                lev = predictions[-1][0].strip()
                state_update = span2state(lev, api_names)
                for api_name in state_update:
                    active_api = api_name
                    if api_name not in dialogue_state:
                        dialogue_state[api_name] = state_update[api_name]
                    else:
                        dialogue_state[api_name].update(state_update[api_name])

                #### save latest state
                state_to_record = copy.deepcopy(dialogue_state)
                state_to_record = {r_en_API_MAP.get(k, k): v for k, v in state_to_record.items()}
                bitod_preds[dial_id]["turns"][str(turn_id)]["state"] = state_to_record
                ####

            elif train_target == 'api':
                new_knowledge_text = 'null'
                do_api_call = predictions[-1][0].strip()

                if do_api_call == 'yes':
                    # make api call
                    api_name = active_api

                    if api_name in dialogue_state:
                        constraints = state2constraints(dialogue_state[api_name])
                        # domain = api_name.split(" ")[0]
                        knowledge = defaultdict(dict)

                        try:
                            msg = api.call_api(r_en_API_MAP.get(api_name, api_name), constraints=[constraints])
                        except Exception as e:
                            logger.error(f'Error: {e}')
                            logger.error(
                                f'Failed API call with api_name: {api_name}, constraints: {constraints},'
                                f' processed_query: {msg[2]}, for turn: {dial_id}/{turn_id}'
                            )
                            msg = [0, 0, 0]

                        if int(msg[1]) <= 0:
                            logger.warning(
                                f'Message = No item available for api_name: {api_name}, constraints: {constraints},'
                                f' processed_query: {msg[2]}, for turn: {dial_id}/{turn_id}'
                            )

                            new_knowledge_text = f'( {api_name} ) Message = No item available.'
                        else:
                            # always choose highest ranking results (having deterministic api results)
                            knowledge[api_name].update(msg[0])
                            new_knowledge_text = knowledge2span(knowledge)

                        #### save latest api constraints
                        bitod_preds[dial_id]["API"][r_en_API_MAP.get(api_name, api_name)] = copy.deepcopy(constraints)
                        ####

                #### save latest api results and constraints
                bitod_preds[dial_id]["turns"][str(turn_id)]["api"] = new_knowledge_text
                ####

            if train_target == 'response':
                # turn dialogue acts into actual responses
                if e2e_args.nlg_type == 'neural':
                    numericalized_turn = numericalize_example(predictions[-1][0], e2e_numericalizer, turn_id, device)
                    generated = generate(nlg_model, nlg_args, numericalized_turn, hyperparameter_idx)

                    partial_batch_prediction_ids = generated.sequences

                    partial_batch_prediction = nlg_numericalizer.reverse(partial_batch_prediction_ids, 'answer')[0]

                    # post-process predictions
                    partial_batch_prediction = nlg_task.postprocess_prediction(turn_id, partial_batch_prediction)
                else:
                    partial_batch_prediction = nlg_model.generate(predictions[-1][0])

                nlg_responses.append(partial_batch_prediction)

                #### save latest response
                bitod_preds[dial_id]["turns"][str(turn_id)]["response"] = nlg_responses[-1]
                ####

                convo_history.append('SYSTEM: ' + predictions[-1][0])

        except KeyboardInterrupt:
            break

    with open(f"{int(time.time())}_bitod_preds.json", 'w') as fout:
        ujson.dump(bitod_preds, fout, indent=2, ensure_ascii=False)
