from .base_model import Base_model
from keras.optimizers import Adam
from keras.models import Model
from keras_alfnet.parallel_model import ParallelModel
from keras.utils import generic_utils
from keras_alfnet import losses as losses
from keras_alfnet import bbox_process
from . import model_alf
import numpy as np
import time, os, cv2


class Model_3step(Base_model):
    def name(self):
        return 'Model_3step'

    def initialize(self, opt):
        Base_model.initialize(self, opt)
        # specify the training details
        self.cls_loss_r1 = []
        self.regr_loss_r1 = []
        self.cls_loss_r2 = []
        self.regr_loss_r2 = []
        self.cls_loss_r3 = []
        self.regr_loss_r3 = []
        self.losses = np.zeros((self.epoch_length, 6))
        self.optimizer = Adam(lr=opt.init_lr)
        print 'Initializing the {}'.format(self.name())

    def creat_model(self, opt, train_data, phase='train'):
        Base_model.create_base_model(self, opt, train_data, phase=phase)
        alf1, alf2, alf3 = model_alf.create_alf(self.base_layers, self.num_anchors, trainable=True, steps=3)
        if phase == 'train':
            self.model_1st = Model(self.img_input, alf1)
            self.model_2nd = Model(self.img_input, alf2)
            self.model_3rd = Model(self.img_input, alf3)
            if self.num_gpus > 1:
                self.model_1st = ParallelModel(self.model_1st, int(self.num_gpus))
                self.model_2nd = ParallelModel(self.model_2nd, int(self.num_gpus))
                self.model_3rd = ParallelModel(self.model_3rd, int(self.num_gpus))
            self.model_1st.compile(optimizer=self.optimizer, loss=[losses.cls_loss, losses.regr_loss],
                                   sample_weight_mode=None)
            self.model_2nd.compile(optimizer=self.optimizer, loss=[losses.cls_loss, losses.regr_loss],
                                   sample_weight_mode=None)
            self.model_3rd.compile(optimizer=self.optimizer, loss=[losses.cls_loss, losses.regr_loss],
                                   sample_weight_mode=None)
        self.model_all = Model(self.img_input, alf1 + alf2 + alf3)

    def train_model(self, opt, weight_path, out_path):
        self.model_all.load_weights(weight_path, by_name=True)
        print 'load weights from {}'.format(weight_path)
        iter_num = 0
        start_time = time.time()
        for epoch_num in range(self.num_epochs):
            progbar = generic_utils.Progbar(self.epoch_length)
            print('Epoch {}/{}'.format(epoch_num + 1 + self.add_epoch, self.num_epochs + self.add_epoch))
            while True:
                try:
                    X, Y, img_data = next(self.data_gen_train)
                    loss_s1 = self.model_1st.train_on_batch(X, Y)
                    self.losses[iter_num, 0] = loss_s1[1]
                    self.losses[iter_num, 1] = loss_s1[2]
                    pred1 = self.model_1st.predict_on_batch(X)
                    Y2 = bbox_process.get_target_1st(self.anchors, pred1[1], img_data, opt,
                                                     igthre=opt.ig_overlap, posthre=opt.pos_overlap_step2,
                                                     negthre=opt.neg_overlap_step2)
                    loss_s2 = self.model_2nd.train_on_batch(X, Y2)
                    self.losses[iter_num, 2] = loss_s2[1]
                    self.losses[iter_num, 3] = loss_s2[2]
                    pred2 = self.model_2nd.predict_on_batch(X)
                    anchors_2nd = bbox_process.generate_pp_2nd(self.anchors, pred1[1], opt)
                    Y3 = bbox_process.get_target_2nd(anchors_2nd, pred2[1], img_data, opt,
                                                     igthre=opt.ig_overlap, posthre=opt.pos_overlap_step3,
                                                     negthre=opt.neg_overlap_step3)
                    loss_s3 = self.model_3rd.train_on_batch(X, Y3)
                    self.losses[iter_num, 4] = loss_s3[1]
                    self.losses[iter_num, 5] = loss_s3[2]

                    iter_num += 1
                    if iter_num % 20 == 0:
                        progbar.update(iter_num,
                                       [('cls1', np.mean(self.losses[:iter_num, 0])),
                                        ('regr1', np.mean(self.losses[:iter_num, 1])),
                                        ('cls2', np.mean(self.losses[:iter_num, 2])),
                                        ('regr2', np.mean(self.losses[:iter_num, 3])),
                                        ('cls3', np.mean(self.losses[:iter_num, 4])),
                                        ('regr3', np.mean(self.losses[:iter_num, 5]))])
                    if iter_num == self.epoch_length:
                        cls_loss1 = np.mean(self.losses[:, 0])
                        regr_loss1 = np.mean(self.losses[:, 1])
                        cls_loss2 = np.mean(self.losses[:, 2])
                        regr_loss2 = np.mean(self.losses[:, 3])
                        cls_loss3 = np.mean(self.losses[:, 4])
                        regr_loss3 = np.mean(self.losses[:, 5])
                        total_loss = cls_loss1 + regr_loss1 + cls_loss2 + regr_loss2 + cls_loss3 + regr_loss3

                        self.total_loss_r.append(total_loss)
                        self.cls_loss_r1.append(cls_loss1)
                        self.regr_loss_r1.append(regr_loss1)
                        self.cls_loss_r2.append(cls_loss2)
                        self.regr_loss_r2.append(regr_loss2)
                        self.cls_loss_r3.append(cls_loss3)
                        self.regr_loss_r3.append(regr_loss3)

                        print('Total loss: {}'.format(total_loss))
                        print('Elapsed time: {}'.format(time.time() - start_time))

                        iter_num = 0
                        start_time = time.time()

                        if total_loss < self.best_loss:
                            print(
                                'Total loss decreased from {} to {}, saving weights'.format(self.best_loss, total_loss))
                            self.best_loss = total_loss
                        self.model_all.save_weights(
                            os.path.join(out_path,
                                         'resnet_e{}_l{}.hdf5'.format(epoch_num + 1 + self.add_epoch, total_loss)))
                        break
                except Exception as e:
                    print ('Exception: {}'.format(e))
                    continue
            records = np.concatenate((np.asarray(self.total_loss_r).reshape((-1, 1)),
                                      np.asarray(self.cls_loss_r1).reshape((-1, 1)),
                                      np.asarray(self.regr_loss_r1).reshape((-1, 1)),
                                      np.asarray(self.cls_loss_r2).reshape((-1, 1)),
                                      np.asarray(self.regr_loss_r2).reshape((-1, 1)),
                                      np.asarray(self.cls_loss_r3).reshape((-1, 1)),
                                      np.asarray(self.regr_loss_r3).reshape((-1, 1))),
                                     axis=-1)
            np.savetxt(os.path.join(out_path, 'records.txt'), np.array(records), fmt='%.6f')
        print('Training complete, exiting.')

    def test_model(self, opt, val_data, weight_path, out_path):
        self.model_all.load_weights(weight_path, by_name=True)
        print 'load weights from {}'.format(weight_path)
        res_all = []
        res_file = os.path.join(out_path, 'val_det.txt')
        start_time = time.time()
        for f in range(len(val_data)):
            filepath = val_data[f]['filepath']
            frame_number = f + 1
            img = cv2.imread(filepath)
            x_in = bbox_process.format_img(img, opt)
            Y = self.model_all.predict(x_in)
            proposals = bbox_process.pred_pp_1st(self.anchors, Y[0], Y[1], opt)
            proposals_2nd = bbox_process.pred_pp_2nd(proposals, Y[2], Y[3], opt)
            bbx, scores = bbox_process.pred_det(proposals_2nd, Y[4], Y[5], opt, step=3)
            f_res = np.repeat(frame_number, len(bbx), axis=0).reshape((-1, 1))
            bbx[:, [2, 3]] -= bbx[:, [0, 1]]
            res_all += np.concatenate((f_res, bbx, scores), axis=-1).tolist()
        np.savetxt(res_file, np.array(res_all), fmt='%.4f')
        print 'Test time: %.4f s' % (time.time() - start_time)
