import io
import os
import re
import sys
import time
import unicodedata

import numpy as np
import tensorflow as tf
from sklearn.model_selection import train_test_split
import functools
import requests

#
# This code sample is copied from the tensorflow docs at
# https://www.tensorflow.org/tutorials/text/nmt_with_attention
#

# Converts the unicode file to ascii
def unicode_to_ascii(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def preprocess_sentence(w):
    w = unicode_to_ascii(w.lower().strip())

    # creating a space between a word and the punctuation following it
    # eg: "he is a boy." => "he is a boy ."
    # Reference:- https://stackoverflow.com/questions/3645931/python-padding-punctuation-with-white-spaces-keeping-punctuation
    w = re.sub(r"([?.!,¿])", r" \1 ", w)
    w = re.sub(r'[" "]+', " ", w)

    # replacing everything with space except (a-z, A-Z, ".", "?", "!", ",")
    w = re.sub(r"[^a-zA-Z?.!,¿]+", " ", w)

    w = w.strip()

    # adding a start and an end token to the sentence
    # so that the model know when to start and stop predicting.
    w = '<start> ' + w + ' <end>'
    return w

def loss_function(real, pred):
    loss_object = tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True, reduction='none')

    mask = tf.math.logical_not(tf.math.equal(real, 0))
    loss_ = loss_object(real, pred)

    mask = tf.cast(mask, dtype=loss_.dtype)
    loss_ *= mask

    return tf.reduce_mean(loss_)


def tokenize(lang):
    lang_tokenizer = tf.keras.preprocessing.text.Tokenizer(
        filters='')
    lang_tokenizer.fit_on_texts(lang)
    tensor = lang_tokenizer.texts_to_sequences(lang)
    tensor = tf.keras.preprocessing.sequence.pad_sequences(tensor, padding='post')

    return tensor, lang_tokenizer

class Encoder(tf.keras.Model):
    def __init__(self, vocab_size, embedding_dim, enc_units, batch_sz):
        super(Encoder, self).__init__()
        self.batch_sz = batch_sz
        self.enc_units = enc_units
        self.embedding = tf.keras.layers.Embedding(vocab_size, embedding_dim)
        self.gru = tf.keras.layers.GRU(self.enc_units,
                                       return_sequences=True,
                                       return_state=True,
                                       recurrent_initializer='glorot_uniform')

    def call(self, x, hidden):
        x = self.embedding(x)
        output, state = self.gru(x, initial_state=hidden)
        return output, state

    def initialize_hidden_state(self):
        return tf.zeros((self.batch_sz, self.enc_units))


class BahdanauAttention(tf.keras.layers.Layer):
    def __init__(self, units):
        super(BahdanauAttention, self).__init__()
        self.W1 = tf.keras.layers.Dense(units)
        self.W2 = tf.keras.layers.Dense(units)
        self.V = tf.keras.layers.Dense(1)

    def call(self, query, values):
        # query hidden state shape == (batch_size, hidden size)
        # query_with_time_axis shape == (batch_size, 1, hidden size)
        # values shape == (batch_size, max_len, hidden size)
        # we are doing this to broadcast addition along the time axis to calculate the score
        query_with_time_axis = tf.expand_dims(query, 1)

        # score shape == (batch_size, max_length, 1)
        # we get 1 at the last axis because we are applying score to self.V
        # the shape of the tensor before applying self.V is (batch_size, max_length, units)
        score = self.V(tf.nn.tanh(
            self.W1(query_with_time_axis) + self.W2(values)))

        # attention_weights shape == (batch_size, max_length, 1)
        attention_weights = tf.nn.softmax(score, axis=1)

        # context_vector shape after sum == (batch_size, hidden_size)
        context_vector = attention_weights * values
        context_vector = tf.reduce_sum(context_vector, axis=1)

        return context_vector, attention_weights


class Decoder(tf.keras.Model):
    def __init__(self, vocab_size, embedding_dim, dec_units, batch_sz):
        super(Decoder, self).__init__()
        self.batch_sz = batch_sz
        self.dec_units = dec_units
        self.embedding = tf.keras.layers.Embedding(vocab_size, embedding_dim)
        self.gru = tf.keras.layers.GRU(self.dec_units,
                                       return_sequences=True,
                                       return_state=True,
                                       recurrent_initializer='glorot_uniform')
        self.fc = tf.keras.layers.Dense(vocab_size)

        # used for attention
        self.attention = BahdanauAttention(self.dec_units)

    def call(self, x, hidden, enc_output):
        # enc_output shape == (batch_size, max_length, hidden_size)
        context_vector, attention_weights = self.attention(hidden, enc_output)

        # x shape after passing through embedding == (batch_size, 1, embedding_dim)
        x = self.embedding(x)

        # x shape after concatenation == (batch_size, 1, embedding_dim + hidden_size)
        x = tf.concat([tf.expand_dims(context_vector, 1), x], axis=-1)

        # passing the concatenated vector to the GRU
        output, state = self.gru(x)

        # output shape == (batch_size * 1, hidden_size)
        output = tf.reshape(output, (-1, output.shape[2]))

        # output shape == (batch_size, vocab)
        x = self.fc(output)

        return x, state, attention_weights





class TranslatorModel:
    BATCH_SIZE = 64
    embedding_dim = 256
    units = 1024
    url = 'http://storage.googleapis.com/download.tensorflow.org/data'


    def __init__(self, lang='eng-fra'):
        self.lang = lang
        self.optimizer = tf.keras.optimizers.Adam()

        self.checkpoint_dir = "/checkpoints/{}".format(lang)
        self.checkpoint_prefix = os.path.join(self.checkpoint_dir, "ckpt")


        self._load(num_examples = 30000)

        self.encoder = Encoder(self.vocab_inp_size, self.embedding_dim, self.units, self.BATCH_SIZE)
        self.decoder = Decoder(self.vocab_tar_size, self.embedding_dim, self.units, self.BATCH_SIZE)



        self.checkpoint = tf.train.Checkpoint(optimizer=self.optimizer,
                                        encoder=self.encoder,
                                        decoder=self.decoder)

        # restoring the latest checkpoint in checkpoint_dir
        self.checkpoint.restore(tf.train.latest_checkpoint(self.checkpoint_dir))


    def _load_dataset(self, num_examples = None):
        lang1, lang2 = self.lang.split('-')
        if self.lang.startswith('eng-'):
            lang2, lang1 = self.lang.split('-')

        path_to_file = "/code/data/{}.txt".format(lang1)
        if not os.path.isfile(path_to_file):
            archive = '{}-{}.zip'.format(lang1, lang2)
            # Download the file
            path_to_zip = tf.keras.utils.get_file(
                archive,
                origin='{}/{}'.format(self.url, archive),
                extract=True)

            path_to_file = os.path.dirname(path_to_zip)+"/{}.txt".format(lang1)

        # load dataset
        lines = io.open(path_to_file, encoding='UTF-8').read().strip().split('\n')
        if num_examples is None:
            num_examples = len(lines)
        word_pairs = [[preprocess_sentence(w) for w in l.split(
            '\t')] for l in lines[:num_examples]]

        return zip(*word_pairs)


    def _load(self, num_examples = None):
        # creating cleaned input, output pairs
        if self.lang.startswith('eng-'):
            self.inp_lang, self.targ_lang = self._load_dataset(num_examples)
        else:
            self.targ_lang, self.inp_lang = self._load_dataset(num_examples)

        self.input_tensor, self.inp_lang_tokenizer = tokenize(self.inp_lang)
        self.target_tensor, self.targ_lang_tokenizer = tokenize(self.targ_lang)

        # Calculate max_length of the target tensors
        self.max_length_targ = self.target_tensor.shape[1]
        self.max_length_inp = self.input_tensor.shape[1]
        self.vocab_inp_size = len(self.inp_lang_tokenizer.word_index)+1
        self.vocab_tar_size = len(self.targ_lang_tokenizer.word_index)+1

        # Creating training and validation sets using an 80-20 split
        input_tensor_train, input_tensor_val, target_tensor_train, target_tensor_val = train_test_split(
            self.input_tensor, self.target_tensor, test_size=0.2)

        self.BUFFER_SIZE = len(input_tensor_train)
        self.steps_per_epoch = len(input_tensor_train)//self.BATCH_SIZE


        dataset = tf.data.Dataset.from_tensor_slices(
            (input_tensor_train, target_tensor_train)).shuffle(self.BUFFER_SIZE)
        self.dataset = dataset.batch(self.BATCH_SIZE, drop_remainder=True)

    @tf.function
    def train_step(self, inp, targ, enc_hidden):
        loss = 0

        with tf.GradientTape() as tape:
            enc_output, enc_hidden = self.encoder(inp, enc_hidden)
            dec_hidden = enc_hidden
            dec_input = tf.expand_dims(
                [self.targ_lang_tokenizer.word_index['<start>']] * self.BATCH_SIZE, 1)

            # Teacher forcing - feeding the target as the next input
            for t in range(1, targ.shape[1]):
                # passing enc_output to the decoder
                predictions, dec_hidden, _ = self.decoder(
                    dec_input, dec_hidden, enc_output)

                loss += loss_function(targ[:, t], predictions)

                # using teacher forcing
                dec_input = tf.expand_dims(targ[:, t], 1)

        batch_loss = (loss / int(targ.shape[1]))
        variables = self.encoder.trainable_variables + self.decoder.trainable_variables
        gradients = tape.gradient(loss, variables)
        self.optimizer.apply_gradients(zip(gradients, variables))
        return batch_loss


    def train(self, epochs = 10):
        for epoch in range(epochs):
            start = time.time()

            enc_hidden = self.encoder.initialize_hidden_state()
            total_loss = 0

            for (batch, (inp, targ)) in enumerate(self.dataset.take(self.steps_per_epoch)):
                batch_loss = self.train_step(inp, targ, enc_hidden)
                total_loss += batch_loss

                if batch % 100 == 0:
                    print('Epoch {} Batch {} Loss {:.4f}'.format(epoch + 1, batch, batch_loss.numpy()))
            # saving (checkpoint) the model every 2 epochs
            if (epoch + 1) % 2 == 0:
                self.checkpoint.save(file_prefix = self.checkpoint_prefix)

            print('Epoch {} Loss {:.4f}'.format(epoch + 1, total_loss / self.steps_per_epoch))
            print('Time taken for 1 epoch {} sec\n'.format(time.time() - start))





    def evaluate(self, sentence):
        sentence = preprocess_sentence(sentence)

        inputs = [self.inp_lang_tokenizer.word_index[i] for i in sentence.split(' ')]
        inputs = tf.keras.preprocessing.sequence.pad_sequences([inputs],
                                                            maxlen=self.max_length_inp,
                                                            padding='post')
        inputs = tf.convert_to_tensor(inputs)

        result = ''

        hidden = [tf.zeros((1, self.units))]
        enc_out, enc_hidden = self.encoder(inputs, hidden)

        dec_hidden = enc_hidden
        dec_input = tf.expand_dims([self.targ_lang_tokenizer.word_index['<start>']], 0)

        for _ in range(self.max_length_targ):
            predictions, dec_hidden, attention_weights = self.decoder(dec_input,
                                                                dec_hidden,
                                                                enc_out)
            predicted_id = tf.argmax(predictions[0]).numpy()
            if self.targ_lang_tokenizer.index_word[predicted_id] == '<end>':
                break
            result += self.targ_lang_tokenizer.index_word[predicted_id] + ' '
            # the predicted ID is fed back into the model
            dec_input = tf.expand_dims([predicted_id], 0)

        return result, sentence



    def translate(self, sentence):
        result, _ = self.evaluate(sentence)

        print('Input: %s' % (sentence))
        print('Predicted translation: {}'.format(result))
        return result





if __name__ == '__main__':
    lang = "eng-fra"
    if len(sys.argv) > 1:
        lang = sys.argv[1]

    if len(sys.argv) > 2:
        epochs = int(sys.argv[2])

    model = TranslatorModel(lang)
    epochs = 10
    print("Translator services is requested to reload the model every 10 epochs.")
    while True:
        model.train(epochs)
        print("Checkpoints saved in {}".format(model.checkpoint_dir))
        response = requests.get("http://translator:5000/reload", params={'lang': lang})
        print("Requested translator service to reload its model, response status: ", response.status_code)
    print("Exiting...")


