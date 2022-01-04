import soundfile as sf
import torch
from numpy import trim_zeros

from Preprocessing.AudioPreprocessor import AudioPreprocessor
from TrainingInterfaces.Text_to_Spectrogram.FastSpeech2.EnergyCalculator import EnergyCalculator
from TrainingInterfaces.Text_to_Spectrogram.FastSpeech2.PitchCalculator import Dio


class ProsodicConditionExtractor:

    def __init__(self, sr):
        self.dio = Dio(reduction_factor=1, fs=16000, use_token_averaged_f0=False)
        self.energy_calc = EnergyCalculator(reduction_factor=1, fs=16000, use_token_averaged_energy=False)
        self.ap = AudioPreprocessor(input_sr=sr, output_sr=16000, melspec_buckets=80, hop_length=256, n_fft=1024, cut_silence=False)

    def extract_condition_from_reference_wave(self, wave):
        norm_wave = self.ap.audio_to_wave_tensor(normalize=True, audio=wave)
        norm_wave = torch.tensor(trim_zeros(norm_wave.numpy()))
        energy = self.energy_calc(input_waves=norm_wave.unsqueeze(0), norm_by_average=False)[0].squeeze()
        average_energy = energy[energy[0] != 0.0].mean()
        highest_energy = energy[energy[0] != 0.0].max()
        lowest_energy = energy[energy[0] != 0.0].min()
        std_dev_energy = energy[energy[0] != 0.0].std()
        pitch = self.dio(input_waves=norm_wave.unsqueeze(0), norm_by_average=False)[0].squeeze()
        average_pitch = pitch[pitch[0] != 0.0].mean()
        highest_pitch = pitch[pitch[0] != 0.0].max()
        lowest_pitch = pitch[pitch[0] != 0.0].min()
        std_dev_pitch = pitch[pitch[0] != 0.0].std()
        print(average_energy)
        print(highest_energy)
        print(lowest_energy)
        print(std_dev_energy)
        print(average_pitch)
        print(highest_pitch)
        print(lowest_pitch)
        print(std_dev_pitch)


if __name__ == '__main__':
    wave, sr = sf.read("../../../audios/1.wav")
    ext = ProsodicConditionExtractor(sr=sr)
    ext.extract_condition_from_reference_wave(wave=wave)