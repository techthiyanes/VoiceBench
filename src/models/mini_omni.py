from .base import VoiceAssistant
import os
import lightning as L
import torch
from snac import SNAC
from .src_mini_omni.litgpt import Tokenizer
import numpy as np
from .src_mini_omni.litgpt.generate.base import (
    generate_AA,
    generate_ASR,
    generate_TA,
    generate_TT,
    generate_AT,
    generate_TA_BATCH,
    next_token_batch
)
from .src_mini_omni.litgpt.model import GPT, Config
from lightning.fabric.utilities.load import _lazy_load as lazy_load
from .src_mini_omni.utils.snac_utils import layershift, reconscruct_snac, reconstruct_tensors, get_time_str
import whisper
from huggingface_hub import snapshot_download


torch.set_printoptions(sci_mode=False)


# TODO
text_vocabsize = 151936
text_specialtokens = 64
audio_vocabsize = 4096
audio_specialtokens = 64

padded_text_vocabsize = text_vocabsize + text_specialtokens
padded_audio_vocabsize = audio_vocabsize + audio_specialtokens

_eot = text_vocabsize
_pad_t = text_vocabsize + 1
_input_t = text_vocabsize + 2
_answer_t = text_vocabsize + 3
_asr = text_vocabsize + 4

_eoa = audio_vocabsize
_pad_a = audio_vocabsize + 1
_input_a = audio_vocabsize + 2
_answer_a = audio_vocabsize + 3
_split = audio_vocabsize + 4


def get_input_ids_TA(text, text_tokenizer):
    input_ids_item = [[] for _ in range(8)]
    text_tokens = text_tokenizer.encode(text)
    for i in range(7):
        input_ids_item[i] = [layershift(_pad_a, i)] * (len(text_tokens) + 2) + [
            layershift(_answer_a, i)
        ]
        input_ids_item[i] = torch.tensor(input_ids_item[i]).unsqueeze(0)
    input_ids_item[-1] = [_input_t] + text_tokens.tolist() + [_eot] + [_answer_t]
    input_ids_item[-1] = torch.tensor(input_ids_item[-1]).unsqueeze(0)
    return input_ids_item


def get_input_ids_TT(text, text_tokenizer):
    input_ids_item = [[] for i in range(8)]
    text_tokens = text_tokenizer.encode(text).tolist()

    for i in range(7):
        input_ids_item[i] = torch.tensor(
            [layershift(_pad_a, i)] * (len(text_tokens) + 3)
        ).unsqueeze(0)
    input_ids_item[-1] = [_input_t] + text_tokens + [_eot] + [_answer_t]
    input_ids_item[-1] = torch.tensor(input_ids_item[-1]).unsqueeze(0)

    return input_ids_item


def get_input_ids_whisper(
    mel, leng, whispermodel, device,
    special_token_a=_answer_a, special_token_t=_answer_t,
):

    with torch.no_grad():
        mel = mel.unsqueeze(0).to(device)
        # audio_feature = whisper.decode(whispermodel,mel, options).audio_features
        audio_feature = whispermodel.embed_audio(mel)[0][:leng]

    T = audio_feature.size(0)
    input_ids = []
    for i in range(7):
        input_ids_item = []
        input_ids_item.append(layershift(_input_a, i))
        input_ids_item += [layershift(_pad_a, i)] * T
        input_ids_item += [(layershift(_eoa, i)), layershift(special_token_a, i)]
        input_ids.append(torch.tensor(input_ids_item).unsqueeze(0))
    input_id_T = torch.tensor([_input_t] + [_pad_t] * T + [_eot, special_token_t])
    input_ids.append(input_id_T.unsqueeze(0))
    return audio_feature.unsqueeze(0), input_ids


def get_input_ids_whisper_ATBatch(mel, leng, whispermodel, device):
    with torch.no_grad():
        mel = mel.unsqueeze(0).to(device)
        # audio_feature = whisper.decode(whispermodel,mel, options).audio_features
        audio_feature = whispermodel.embed_audio(mel)[0][:leng]
    T = audio_feature.size(0)
    input_ids_AA = []
    for i in range(7):
        input_ids_item = []
        input_ids_item.append(layershift(_input_a, i))
        input_ids_item += [layershift(_pad_a, i)] * T
        input_ids_item += [(layershift(_eoa, i)), layershift(_answer_a, i)]
        input_ids_AA.append(torch.tensor(input_ids_item))
    input_id_T = torch.tensor([_input_t] + [_pad_t] * T + [_eot, _answer_t])
    input_ids_AA.append(input_id_T)

    input_ids_AT = []
    for i in range(7):
        input_ids_item = []
        input_ids_item.append(layershift(_input_a, i))
        input_ids_item += [layershift(_pad_a, i)] * T
        input_ids_item += [(layershift(_eoa, i)), layershift(_pad_a, i)]
        input_ids_AT.append(torch.tensor(input_ids_item))
    input_id_T = torch.tensor([_input_t] + [_pad_t] * T + [_eot, _answer_t])
    input_ids_AT.append(input_id_T)

    input_ids = [input_ids_AA, input_ids_AT]
    stacked_inputids = [[] for _ in range(8)]
    for i in range(2):
        for j in range(8):
            stacked_inputids[j].append(input_ids[i][j])
    stacked_inputids = [torch.stack(tensors) for tensors in stacked_inputids]
    return torch.stack([audio_feature, audio_feature]), stacked_inputids


def A1_T2(fabric, audio_feature, input_ids, leng, model, text_tokenizer):
    with fabric.init_tensor():
        model.set_kv_cache(batch_size=1)
    tokenlist = generate_AT(
        model,
        audio_feature,
        input_ids,
        [leng],
        ["AT"],
        max_returned_tokens=2048,
        temperature=0.9,
        top_k=1,
        eos_id_a=_eoa,
        eos_id_t=_eot,
        pad_id_t=_pad_t,
        shift=padded_text_vocabsize,
        include_prompt=True,
        generate_text=True,
    )
    return text_tokenizer.decode(torch.tensor(tokenlist)).strip()


def T1_T2(fabric, input_ids, model, text_tokenizer):

    with fabric.init_tensor():
        model.set_kv_cache(batch_size=1)
    tokenlist = generate_TT(
        model,
        None,
        input_ids,
        None,
        ["T1T2"],
        max_returned_tokens=2048,
        temperature=0.9,
        top_k=1,
        eos_id_a=_eoa,
        eos_id_t=_eot,
        pad_id_t=_pad_t,
        shift=padded_text_vocabsize,
        include_prompt=True,
        generate_text=True,
    )
    model.clear_kv_cache()
    return text_tokenizer.decode(torch.tensor(tokenlist)).strip()


class MiniOmniAssistant(VoiceAssistant):
    def __init__(self):
        self.device = "cuda:0"
        self.ckpt_dir = f"./cache/mini-omni"
        self.fabric, self.model, self.text_tokenizer, self.snacmodel, self.whispermodel = self.load_model(self.ckpt_dir, self.device)

    def load_audio(self, audio):
        duration_ms = (len(audio) / 16000) * 1000
        audio = whisper.pad_or_trim(audio)
        mel = whisper.log_mel_spectrogram(audio)
        return mel, int(duration_ms / 20) + 1

    def load_model(self, ckpt_dir, device):
        if not os.path.exists(ckpt_dir):
            print(f"checkpoint directory {ckpt_dir} not found, downloading from huggingface")
            repo_id = "gpt-omni/mini-omni"
            snapshot_download(repo_id, local_dir=ckpt_dir, revision="main")

        snacmodel = SNAC.from_pretrained("hubertsiuzdak/snac_24khz").eval().to(device)
        whispermodel = whisper.load_model("small").to(device)
        text_tokenizer = Tokenizer(ckpt_dir)
        fabric = L.Fabric(devices=1, strategy="auto")
        config = Config.from_file(ckpt_dir + "/model_config.yaml")
        config.post_adapter = False

        with fabric.init_module(empty_init=False):
            model = GPT(config)

        model = fabric.setup(model)
        state_dict = lazy_load(ckpt_dir + "/lit_model.pth")
        model.load_state_dict(state_dict, strict=True)
        model.to(device).eval()

        return fabric, model, text_tokenizer, snacmodel, whispermodel

    def generate_audio(
        self,
        audio,
    ):
        assert audio['sampling_rate'] == 16000
        audio = audio['array'].astype(np.float32)
        mel, leng = self.load_audio(audio)
        audio_feature, input_ids = get_input_ids_whisper(
            mel, leng, self.whispermodel, self.device,
            special_token_a=_pad_a, special_token_t=_answer_t
        )
        response = A1_T2(
            self.fabric, audio_feature, input_ids, leng, self.model, self.text_tokenizer
        )
        return response

    def generate_text(
        self,
        text,
    ):
        input_ids = get_input_ids_TT(text, self.text_tokenizer)
        response = T1_T2(self.fabric, input_ids, self.model, self.text_tokenizer)
        return response
