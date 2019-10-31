import os
import io
import shutil
import argparse

import numpy as np
from PIL import Image
import tensorflow as tf

from keras.callbacks import LearningRateScheduler, TensorBoard, Callback

try:
    from keras.utils.training_utils import multi_gpu_model
except ImportError:
    from keras.utils.multi_gpu_utils import multi_gpu_model
import keras.backend as K

from adamw import AdamW
import data_processor
from model import EAST_model
from losses import dice_loss, rbox_loss

parser = argparse.ArgumentParser()

parser.add_argument('--training_data_path', type=str, default='data/ICDAR2015/train_data')
parser.add_argument('--validation_data_path', type=str, default='data/ICDAR2015/val_data')
parser.add_argument('--checkpoint_path', type=str, default='models/east_v1')

parser.add_argument('--input_size', type=int, default=512)
parser.add_argument('--batch_size', type=int, default=16)
parser.add_argument('--nb_workers', type=int, default=4)
parser.add_argument('--init_learning_rate', type=float, default=0.0001)
parser.add_argument('--lr_decay_rate', type=float, default=0.94)
parser.add_argument('--lr_decay_steps', type=int, default=130)
parser.add_argument('--max_epochs', type=int, default=150)
parser.add_argument('--save_checkpoint_epochs', type=int, default=10)

parser.add_argument('--gpu_list', type=str, default='0')
parser.add_argument('--restore_model', type=str, default='')
parser.add_argument('--max_image_large_side', type=int, default=1280)
parser.add_argument('--max_text_size', type=int, default=800)
parser.add_argument('--min_text_size', type=int, default=10)
parser.add_argument('--min_crop_side_ratio', type=float, default=0.1)
parser.add_argument('--geometry', type=str, default='RBOX')
parser.add_argument('--suppress_warnings_and_error_messages', type=bool, default=True)

FLAGS = parser.parse_args()

gpus = list(range(len(FLAGS.gpu_list.split(','))))
os.system('mkdir -p models')


class CustomModelCheckpoint(Callback):
    def __init__(self, model, path, period, save_weights_only):
        super(CustomModelCheckpoint, self).__init__()
        self.period = period
        self.path = path
        # We set the model (non multi gpu) under an other name
        self.model_for_saving = model
        self.epochs_since_last_save = 0
        self.save_weights_only = save_weights_only

    def on_epoch_end(self, epoch, logs=None):
        self.epochs_since_last_save += 1
        if self.epochs_since_last_save >= self.period:
            self.epochs_since_last_save = 0
            if self.save_weights_only:
                self.model_for_saving.save_weights(self.path.format(epoch=epoch + 1, **logs), overwrite=True)
            else:
                self.model_for_saving.save(self.path.format(epoch=epoch + 1, **logs), overwrite=True)


def make_image_summary(tensor):
    """
    Convert an numpy representation image to Image protobuf.
    Copied from https://github.com/lanpa/tensorboard-pytorch/
    """
    if len(tensor.shape) == 2:
        height, width = tensor.shape
        channel = 1
    else:
        height, width, channel = tensor.shape
        if channel == 1:
            tensor = tensor[:, :, 0]
    image = Image.fromarray(tensor)
    output = io.BytesIO()
    image.save(output, format='PNG')
    image_string = output.getvalue()
    output.close()
    return tf.Summary.Image(height=height,
                            width=width,
                            colorspace=channel,
                            encoded_image_string=image_string)


class CustomTensorBoard(TensorBoard):
    def __init__(self, log_dir, score_map_loss_weight, small_text_weight, data_generator, write_graph=False):
        self.score_map_loss_weight = score_map_loss_weight
        self.small_text_weight = small_text_weight
        self.data_generator = data_generator
        super(CustomTensorBoard, self).__init__(log_dir=log_dir, write_graph=write_graph)

    def on_epoch_end(self, epoch, logs=None):
        logs.update(
            {'learning_rate': K.eval(self.model.optimizer.lr), 'small_text_weight': K.eval(self.small_text_weight)})
        data = next(self.data_generator)
        pred_score_maps, pred_geo_maps = self.model.predict([data[0][0], data[0][1], data[0][2], data[0][3]])
        img_summaries = []
        for i in range(3):
            input_image_summary = make_image_summary(((data[0][0][i] + 1) * 127.5).astype('uint8'))
            overly_small_text_region_training_mask_summary = make_image_summary((data[0][1][i] * 255).astype('uint8'))
            text_region_boundary_training_mask_summary = make_image_summary((data[0][2][i] * 255).astype('uint8'))
            target_score_map_summary = make_image_summary((data[1][0][i] * 255).astype('uint8'))
            pred_score_map_summary = make_image_summary((pred_score_maps[i] * 255).astype('uint8'))
            img_summaries.append(tf.Summary.Value(tag='input_image/%d' % i, image=input_image_summary))
            img_summaries.append(tf.Summary.Value(tag='overly_small_text_region_training_mask/%d' % i,
                                                  image=overly_small_text_region_training_mask_summary))
            img_summaries.append(tf.Summary.Value(tag='text_region_boundary_training_mask/%d' % i,
                                                  image=text_region_boundary_training_mask_summary))
            img_summaries.append(tf.Summary.Value(tag='score_map_target/%d' % i, image=target_score_map_summary))
            img_summaries.append(tf.Summary.Value(tag='score_map_pred/%d' % i, image=pred_score_map_summary))
            for j in range(4):
                target_geo_map_summary = make_image_summary(
                    (data[1][1][i, :, :, j] / FLAGS.input_size * 255).astype('uint8'))
                pred_geo_map_summary = make_image_summary(
                    (pred_geo_maps[i, :, :, j] / FLAGS.input_size * 255).astype('uint8'))
                img_summaries.append(
                    tf.Summary.Value(tag='geo_map_%d_target/%d' % (j, i), image=target_geo_map_summary))
                img_summaries.append(tf.Summary.Value(tag='geo_map_%d_pred/%d' % (j, i), image=pred_geo_map_summary))
            target_geo_map_summary = make_image_summary(((data[1][1][i, :, :, 4] + 1) * 127.5).astype('uint8'))
            pred_geo_map_summary = make_image_summary(((pred_geo_maps[i, :, :, 4] + 1) * 127.5).astype('uint8'))
            img_summaries.append(tf.Summary.Value(tag='geo_map_%d_target/%d' % (4, i), image=target_geo_map_summary))
            img_summaries.append(tf.Summary.Value(tag='geo_map_%d_pred/%d' % (4, i), image=pred_geo_map_summary))
        tf_summary = tf.Summary(value=img_summaries)
        self.writer.add_summary(tf_summary, epoch + 1)
        super(CustomTensorBoard, self).on_epoch_end(epoch + 1, logs)


class SmallTextWeight(Callback):
    def __init__(self, weight):
        self.weight = weight

    # TO BE CHANGED
    def on_epoch_end(self, epoch, logs={}):
        # K.set_value(self.weight, np.minimum(epoch / (0.5 * FLAGS.max_epochs), 1.))
        K.set_value(self.weight, 0)


class ValidationEvaluator(Callback):
    def __init__(self, validation_data, validation_log_dir, period=5):
        super(Callback, self).__init__()

        self.period = period
        self.validation_data = validation_data
        self.validation_log_dir = validation_log_dir
        self.val_writer = tf.summary.FileWriter(self.validation_log_dir)

    def on_epoch_end(self, epoch, logs={}):
        if (epoch + 1) % self.period == 0:
            val_loss, val_score_map_loss, val_geo_map_loss = self.model.evaluate(
                [self.validation_data[0], self.validation_data[1], self.validation_data[2], self.validation_data[3]],
                [self.validation_data[3], self.validation_data[4]],
                batch_size=FLAGS.batch_size)
            print('\nEpoch %d: val_loss: %.4f, val_score_map_loss: %.4f, val_geo_map_loss: %.4f' % (
                epoch + 1, val_loss, val_score_map_loss, val_geo_map_loss))
            val_loss_summary = tf.Summary()
            val_loss_summary_value = val_loss_summary.value.add()
            val_loss_summary_value.simple_value = val_loss
            val_loss_summary_value.tag = 'loss'
            self.val_writer.add_summary(val_loss_summary, epoch + 1)
            val_score_map_loss_summary = tf.Summary()
            val_score_map_loss_summary_value = val_score_map_loss_summary.value.add()
            val_score_map_loss_summary_value.simple_value = val_score_map_loss
            val_score_map_loss_summary_value.tag = 'pred_score_map_loss'
            self.val_writer.add_summary(val_score_map_loss_summary, epoch + 1)
            val_geo_map_loss_summary = tf.Summary()
            val_geo_map_loss_summary_value = val_geo_map_loss_summary.value.add()
            val_geo_map_loss_summary_value.simple_value = val_geo_map_loss
            val_geo_map_loss_summary_value.tag = 'pred_geo_map_loss'
            self.val_writer.add_summary(val_geo_map_loss_summary, epoch + 1)

            pred_score_maps, pred_geo_maps = self.model.predict(
                [self.validation_data[0][0:3], self.validation_data[1][0:3], self.validation_data[2][0:3],
                 self.validation_data[3][0:3]])
            img_summaries = []
            for i in range(3):
                input_image_summary = make_image_summary(((self.validation_data[0][i] + 1) * 127.5).astype('uint8'))
                overly_small_text_region_training_mask_summary = make_image_summary(
                    (self.validation_data[1][i] * 255).astype('uint8'))
                text_region_boundary_training_mask_summary = make_image_summary(
                    (self.validation_data[2][i] * 255).astype('uint8'))
                target_score_map_summary = make_image_summary((self.validation_data[3][i] * 255).astype('uint8'))
                pred_score_map_summary = make_image_summary((pred_score_maps[i] * 255).astype('uint8'))
                img_summaries.append(tf.Summary.Value(tag='input_image/%d' % i, image=input_image_summary))
                img_summaries.append(tf.Summary.Value(tag='overly_small_text_region_training_mask/%d' % i,
                                                      image=overly_small_text_region_training_mask_summary))
                img_summaries.append(tf.Summary.Value(tag='text_region_boundary_training_mask/%d' % i,
                                                      image=text_region_boundary_training_mask_summary))
                img_summaries.append(tf.Summary.Value(tag='score_map_target/%d' % i, image=target_score_map_summary))
                img_summaries.append(tf.Summary.Value(tag='score_map_pred/%d' % i, image=pred_score_map_summary))
                for j in range(4):
                    target_geo_map_summary = make_image_summary(
                        (self.validation_data[4][i, :, :, j] / FLAGS.input_size * 255).astype('uint8'))
                    pred_geo_map_summary = make_image_summary(
                        (pred_geo_maps[i, :, :, j] / FLAGS.input_size * 255).astype('uint8'))
                    img_summaries.append(
                        tf.Summary.Value(tag='geo_map_%d_target/%d' % (j, i), image=target_geo_map_summary))
                    img_summaries.append(
                        tf.Summary.Value(tag='geo_map_%d_pred/%d' % (j, i), image=pred_geo_map_summary))
                target_geo_map_summary = make_image_summary(
                    ((self.validation_data[4][i, :, :, 4] + 1) * 127.5).astype('uint8'))
                pred_geo_map_summary = make_image_summary(((pred_geo_maps[i, :, :, 4] + 1) * 127.5).astype('uint8'))
                img_summaries.append(
                    tf.Summary.Value(tag='geo_map_%d_target/%d' % (4, i), image=target_geo_map_summary))
                img_summaries.append(tf.Summary.Value(tag='geo_map_%d_pred/%d' % (4, i), image=pred_geo_map_summary))
            tf_summary = tf.Summary(value=img_summaries)
            self.val_writer.add_summary(tf_summary, epoch + 1)
            self.val_writer.flush()


def lr_decay(epoch):
    return FLAGS.init_learning_rate * np.power(FLAGS.lr_decay_rate, epoch // FLAGS.lr_decay_steps)


def main(argv=None):
    os.environ['CUDA_VISIBLE_DEVICES'] = FLAGS.gpu_list

    # check if checkpoint path exists
    if not os.path.exists(FLAGS.checkpoint_path):
        os.mkdir(FLAGS.checkpoint_path)
    else:
        # if not FLAGS.restore:
        #    shutil.rmtree(FLAGS.checkpoint_path)
        #    os.mkdir(FLAGS.checkpoint_path)
        shutil.rmtree(FLAGS.checkpoint_path)
        os.mkdir(FLAGS.checkpoint_path)

    train_data_generator = data_processor.generator(FLAGS)
    train_samples_count = len(data_processor.get_image_paths(FLAGS.training_data_path))
    val_data = data_processor.load_val_data(FLAGS)

    if len(gpus) <= 1:
        print('Training with 1 GPU')
        east = EAST_model(FLAGS.input_size)
        parallel_model = east.model
    else:
        print('Training with %d GPUs' % len(gpus))
        with tf.device("/cpu:0"):
            east = EAST_model(FLAGS.input_size)
        if FLAGS.restore_model is not '':
            east.model.load_weights(FLAGS.restore_model)
        parallel_model = multi_gpu_model(east.model, gpus=len(gpus))

    score_map_loss_weight = K.variable(0.01, name='score_map_loss_weight')

    small_text_weight = K.variable(0., name='small_text_weight')

    lr_scheduler = LearningRateScheduler(lr_decay)
    ckpt = CustomModelCheckpoint(model=east.model, path=FLAGS.checkpoint_path + '/model-{epoch:02d}.h5',
                                 period=FLAGS.save_checkpoint_epochs, save_weights_only=True)
    tb = CustomTensorBoard(log_dir=FLAGS.checkpoint_path + '/train', score_map_loss_weight=score_map_loss_weight,
                           small_text_weight=small_text_weight, data_generator=train_data_generator, write_graph=True)
    small_text_weight_callback = SmallTextWeight(small_text_weight)
    validation_evaluator = ValidationEvaluator(val_data, validation_log_dir=FLAGS.checkpoint_path + '/val')
    callbacks = [lr_scheduler, ckpt, tb, small_text_weight_callback, validation_evaluator]

    opt = AdamW(FLAGS.init_learning_rate)

    parallel_model.compile(loss=[
        dice_loss(east.overly_small_text_region_training_mask, east.text_region_boundary_training_mask,
                  score_map_loss_weight, small_text_weight),
        rbox_loss(east.overly_small_text_region_training_mask, east.text_region_boundary_training_mask,
                  small_text_weight, east.target_score_map)],
        loss_weights=[1., 1.],
        optimizer=opt)
    east.model.summary()

    model_json = east.model.to_json()
    with open(FLAGS.checkpoint_path + '/model.json', 'w') as json_file:
        json_file.write(model_json)

    parallel_model.fit_generator(train_data_generator, epochs=FLAGS.max_epochs,
                                 steps_per_epoch=train_samples_count / FLAGS.batch_size,
                                 workers=FLAGS.nb_workers, use_multiprocessing=True, max_queue_size=10,
                                 callbacks=callbacks, verbose=1)


if __name__ == '__main__':
    main()
