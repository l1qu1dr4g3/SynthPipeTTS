import json
import os
import time

import matplotlib.pyplot as plt
import torch
import torch.multiprocessing
from torch.cuda.amp import GradScaler
from torch.cuda.amp import autocast
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data.dataloader import DataLoader
from tqdm import tqdm

from PreprocessingForTTS.ProcessText import TextFrontend
from Utility.WarmupScheduler import WarmupScheduler


def plot_attentions_all_heads(atts, att_dir, step):
    # plot all attention heads in one plot
    fig, axes = plt.subplots(nrows=len(atts) // 2, ncols=2, figsize=(6, 8))
    atts_1 = atts[::2]
    atts_2 = atts[1::2]
    for index, att in enumerate(atts_1):
        axes[index][0].imshow(att.detach().numpy(), interpolation='nearest', aspect='auto', origin="lower")
        axes[index][0].xaxis.set_visible(False)
        axes[index][0].yaxis.set_visible(False)
    for index, att in enumerate(atts_2):
        axes[index][1].imshow(att.detach().numpy(), interpolation='nearest', aspect='auto', origin="lower")
        axes[index][1].xaxis.set_visible(False)
        axes[index][1].yaxis.set_visible(False)
    plt.subplots_adjust(left=0.02, bottom=0.02, right=.98, top=.98, wspace=0, hspace=0)
    if not os.path.exists(os.path.join(att_dir, "atts")):
        os.makedirs(os.path.join(att_dir, "atts"))
    plt.savefig(os.path.join(os.path.join(att_dir, "atts"), str(step) + ".png"))
    plt.clf()
    plt.close()


def plot_attentions_best_head(atts, att_dir, step, phones):
    # plot most diagonal attention head individually
    most_diagonal_att = select_best_att_head(atts)
    plt.figure(figsize=(8, 4))
    plt.imshow(most_diagonal_att.detach().numpy(), interpolation='nearest', aspect='auto', origin="lower")
    plt.xlabel("Inputs")
    plt.ylabel("Outputs")
    plt.xticks(range(len(most_diagonal_att[0])), labels=[phone for phone in phones])
    plt.tight_layout()
    if not os.path.exists(os.path.join(att_dir, "atts_diag")):
        os.makedirs(os.path.join(att_dir, "atts_diag"))
    plt.savefig(os.path.join(os.path.join(att_dir, "atts_diag"), str(step) + ".png"))
    plt.clf()
    plt.close()


def get_atts(model, lang, device, spemb):
    tf = TextFrontend(language=lang, use_panphon_vectors=False, use_word_boundaries=False, use_explicit_eos=False)
    sentence = ""
    if lang == "en":
        sentence = "This is a complex sentence, it even has a pause!"
    elif lang == "de":
        sentence = "Dies ist ein komplexer Satz, er hat sogar eine Pause!"
    text = tf.string_to_tensor(sentence).long().squeeze(0).to(device)
    phones = tf.get_phone_string(sentence)
    model.eval()
    atts = model.inference(text=text, spembs=spemb)[2].to("cpu")
    model.train()
    del tf
    return atts, phones


def select_best_att_head(att_ws):
    att_ws = torch.cat([att_w for att_w in att_ws], dim=0)
    diagonal_scores = att_ws.max(dim=-1)[0].mean(dim=-1)
    diagonal_head_idx = diagonal_scores.argmax()
    att_ws = att_ws[diagonal_head_idx]
    return att_ws


def collate_and_pad(batch):
    if len(batch[0]) == 4:
        # every entry in batch: [text, text_length, spec, spec_length]
        texts = list()
        text_lens = list()
        speechs = list()
        speech_lens = list()
        for datapoint in batch:
            texts.append(torch.LongTensor(datapoint[0]).squeeze(0))
            text_lens.append(torch.LongTensor([datapoint[1]]))
            speechs.append(torch.Tensor(datapoint[2]))
            speech_lens.append(torch.LongTensor([datapoint[3]]))
        return (
            pad_sequence(texts, batch_first=True), torch.stack(text_lens).squeeze(1), pad_sequence(speechs, batch_first=True),
            torch.stack(speech_lens).squeeze(1))
    elif len(batch[0]) == 5:
        # every entry in batch: [text, text_length, spec, spec_length, speaker_embedding]
        texts = list()
        text_lens = list()
        speechs = list()
        speech_lens = list()
        spembs = list()
        for datapoint in batch:
            texts.append(torch.LongTensor(datapoint[0]).squeeze(0))
            text_lens.append(torch.LongTensor([datapoint[1]]))
            speechs.append(torch.Tensor(datapoint[2]))
            speech_lens.append(torch.LongTensor([datapoint[3]]))
            spembs.append(torch.Tensor(datapoint[4]))
        return (
            pad_sequence(texts, batch_first=True), torch.stack(text_lens).squeeze(1), pad_sequence(speechs, batch_first=True),
            torch.stack(speech_lens).squeeze(1),
            torch.stack(spembs))


def train_loop(net,
               train_dataset,
               valid_dataset,
               device,
               save_directory,
               batch_size=32,
               steps=400000,
               gradient_accumulation=1,
               epochs_per_save=10,
               use_speaker_embedding=False,
               lang="en",
               lr=0.1,
               warmup_steps=14000,
               path_to_checkpoint=None,
               fine_tune=False):
    """
    :param steps: How many steps to train
    :param lr: The initial learning rate for the optimiser
    :param warmup_steps: how many warmup steps for the warmup scheduler
    :param path_to_checkpoint: reloads a checkpoint to continue training from there
    :param fine_tune: whether to load everything from a checkpoint, or only the model parameters
    :param lang: language of the synthesis
    :param use_speaker_embedding: whether to expect speaker embeddings
    :param net: Model to train
    :param train_dataset: Pytorch Dataset Object for train data
    :param valid_dataset: Pytorch Dataset Object for validation data
    :param device: Device to put the loaded tensors on
    :param save_directory: Where to save the checkpoints
    :param batch_size: How many elements should be loaded at once
    :param gradient_accumulation: how many batches to average before stepping
    :param epochs_per_save: how many epochs to train in between checkpoints
    """
    net = net.to(device)
    scaler = GradScaler()
    train_loader = DataLoader(batch_size=batch_size, dataset=train_dataset, drop_last=True, num_workers=8, pin_memory=False, shuffle=True, prefetch_factor=8,
                              collate_fn=collate_and_pad, persistent_workers=True)
    valid_loader = DataLoader(batch_size=10, dataset=valid_dataset, drop_last=False, num_workers=5, pin_memory=False, prefetch_factor=2,
                              collate_fn=collate_and_pad, persistent_workers=True)

    loss_plot = [[], []]
    if use_speaker_embedding:
        reference_spemb_for_att_plot = torch.Tensor(valid_dataset[0][4]).to(device)
    else:
        reference_spemb_for_att_plot = None
    step_counter = 0
    epoch = 0
    net.train()
    optimizer = torch.optim.Adam(net.parameters(), lr=lr)
    scheduler = WarmupScheduler(optimizer, warmup_steps=warmup_steps)

    if path_to_checkpoint is not None:
        # careful when restarting, plotting data will be overwritten!
        check_dict = torch.load(os.path.join(path_to_checkpoint), map_location=device)
        net.load_state_dict(check_dict["model"])
        if not fine_tune:
            optimizer.load_state_dict(check_dict["optimizer"])
            scaler.load_state_dict(check_dict["scaler"])
            scheduler.load_state_dict(check_dict["scheduler"])
            if "step_counter" in check_dict:
                step_counter = check_dict["step_counter"]
            else:
                # legacy support
                step_counter = int(path_to_checkpoint.split(".")[0].split("_")[-1])

    start_time = time.time()
    while True:
        # train one epoch
        epoch += 1
        grad_accum = 0
        optimizer.zero_grad()
        train_losses_this_epoch = list()
        for train_datapoint in tqdm(train_loader):
            if gradient_accumulation == 1:
                with autocast():
                    if not use_speaker_embedding:
                        train_loss = net(train_datapoint[0].to(device), train_datapoint[1].to(device), train_datapoint[2].to(device),
                                         train_datapoint[3].to(device))
                    else:
                        train_loss = net(train_datapoint[0].to(device), train_datapoint[1].to(device), train_datapoint[2].to(device),
                                         train_datapoint[3].to(device), train_datapoint[4].to(device))
                    train_losses_this_epoch.append(float(train_loss))
                scaler.scale(train_loss).backward()
                del train_loss
                grad_accum += 1
                if grad_accum % gradient_accumulation == 0:
                    grad_accum = 0
                    step_counter += 1
                    # update weights
                    # print("Step: {}".format(step_counter))
                    torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
                    scaler.step(optimizer)
                    scaler.update()
                    scheduler.step()
                    optimizer.zero_grad()
                    torch.cuda.empty_cache()
            else:
                if not use_speaker_embedding:
                    train_loss = net(train_datapoint[0].to(device), train_datapoint[1].to(device), train_datapoint[2].to(device), train_datapoint[3].to(device))
                else:
                    train_loss = net(train_datapoint[0].to(device), train_datapoint[1].to(device), train_datapoint[2].to(device), train_datapoint[3].to(device),
                                     train_datapoint[4].to(device))
                train_losses_this_epoch.append(float(train_loss))
                (train_loss / gradient_accumulation).backward()
                del train_loss
                grad_accum += 1
                if grad_accum % gradient_accumulation == 0:
                    grad_accum = 0
                    step_counter += 1
                    # update weights
                    # print("Step: {}".format(step_counter))
                    torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
                    optimizer.step()
                    scheduler.step()
                    optimizer.zero_grad()
                    torch.cuda.empty_cache()

        # evaluate on valid after every epoch
        with torch.no_grad():
            net.eval()
            val_losses = list()
            for validation_datapoint in valid_loader:
                if not use_speaker_embedding:
                    val_losses.append(float(net(validation_datapoint[0].to(device), validation_datapoint[1].to(device), validation_datapoint[2].to(device),
                                                validation_datapoint[3].to(device))))
                else:
                    val_losses.append(float(net(validation_datapoint[0].to(device), validation_datapoint[1].to(device), validation_datapoint[2].to(device),
                                                validation_datapoint[3].to(device), validation_datapoint[4].to(device))))
            average_val_loss = sum(val_losses) / len(val_losses)
            if epoch % epochs_per_save == 0:
                torch.save({
                    "model"    : net.state_dict(), "optimizer": optimizer.state_dict(), "scaler": scaler.state_dict(), "step_counter": step_counter,
                    "scheduler": scheduler.state_dict()
                    }, os.path.join(save_directory, "checkpoint_{}.pt".format(step_counter)))
                all_atts, phones = get_atts(model=net, lang=lang, device=device, spemb=reference_spemb_for_att_plot)
                plot_attentions_all_heads(torch.cat([att_w for att_w in all_atts], dim=0), att_dir=save_directory, step=step_counter)
                plot_attentions_best_head(all_atts, att_dir=save_directory, step=step_counter, phones=phones)
                if step_counter > steps:
                    # DONE
                    return
            print("Epoch:        {}".format(epoch + 1))
            print("Train Loss:   {}".format(sum(train_losses_this_epoch) / len(train_losses_this_epoch)))
            print("Valid Loss:   {}".format(average_val_loss))
            print("Time elapsed: {} Minutes".format(round((time.time() - start_time) / 60), 2))
            print("Steps:        {}".format(step_counter))
            loss_plot[0].append(sum(train_losses_this_epoch) / len(train_losses_this_epoch))
            loss_plot[1].append(average_val_loss)
            with open(os.path.join(save_directory, "train_val_loss.json"), 'w') as plotting_data_file:
                json.dump(loss_plot, plotting_data_file)
            net.train()
