
"""
@author: danfeng
"""
#import library
import numpy as np
import matplotlib.pyplot as plt

import tensorflow as tf
# tf.disable_eager_execution()
tf.compat.v1.disable_eager_execution()
# tf.disable_v2_behavior()

import scipy.io as scio 
import scipy.io as sio
from tf_utils import random_mini_batches_GCN
from tensorflow.python.framework import ops

import random
import threading
import psutil, GPUtil
import time
import datetime
import pandas as pd

ini_rc, ini_wc, ini_rb, ini_wb = psutil.disk_io_counters()[:4]
ini_bs, ini_br = psutil.net_io_counters()[:2]

def sample_metrics(unit="MB"):
    global ini_rc, ini_wc, ini_rb, ini_wb, ini_bs, ini_br

    weight = 1
    if unit == "MB":
        weight = 1024 * 1024
    elif unit == "GB":
        weight = 1024 * 1024 * 1024
    network_stat = psutil.net_io_counters()
    disk_io_stat = psutil.disk_io_counters()
    result = {
        "time": str(datetime.datetime.utcnow()),
        "cpu": psutil.cpu_percent(interval=1),
        "mem": psutil.virtual_memory().used / weight,
        "ram": psutil.virtual_memory().active / weight,
        "disk": psutil.disk_usage("/").used / weight,
        "disk_io": {
            "rc": disk_io_stat[0] - ini_rc,
            "wc": disk_io_stat[1] - ini_wc,
            "rb": disk_io_stat[2] - ini_rb,
            "wb": disk_io_stat[3] - ini_wb
        },
        "network": {
            "sent": network_stat.bytes_sent / weight - ini_bs,
            "recv": network_stat.bytes_recv / weight - ini_br
        }
    }
    # if self._use_gpu:
    gpus = GPUtil.getGPUs()
    if len(gpus) > 0:
        result["gpu load"] = gpus[0].load * 100
        result["gpu memutil"] = gpus[0].memoryUtil * 100
    return result

def compute_metrics():
    global running
    running = True
    currentProcess = psutil.Process()

    lst = []
    # start loop
    while running:
        # *measure/store all needed metrics*
        lst.append(sample_metrics())
        time.sleep(1)
    df = pd.DataFrame(lst)
    df.to_csv('hs_minigcn_metrics.csv', index=False)

def start():
    global t
    # create thread and start it
    t = threading.Thread(target=compute_metrics)
    t.start()

def stop():
    global running
    global t
    # use `running` to stop loop in thread so thread will end
    running = False
    # wait for thread's end
    t.join()

def convert_to_one_hot(Y, C):
    Y = np.eye(C)[Y.reshape(-1)].T
    return Y

def create_placeholders(n_x, n_y):

    isTraining = tf.compat.v1.placeholder_with_default(True, shape=())
    x_in = tf.compat.v1.placeholder(tf.float32,  [None, n_x], name = "x_in")
    y_in = tf.compat.v1.placeholder(tf.float32, [None, n_y], name = "y_in")
    lap_train = tf.compat.v1.placeholder(tf.float32, [None, None], name = "lap_train")
    
    return x_in, y_in, lap_train, isTraining

def initialize_parameters():
   
    tf.compat.v1.set_random_seed(1) # tf.random.set_seed(1)

    x_w1 = tf.compat.v1.get_variable("x_w1", [200,128], initializer = tf.compat.v1.keras.initializers.VarianceScaling(scale=1.0, mode="fan_avg", distribution="uniform", seed = 1))
    x_b1 = tf.compat.v1.get_variable("x_b1", [128], initializer = tf.compat.v1.zeros_initializer())

    x_w2 = tf.compat.v1.get_variable("x_w2", [128,16], initializer = tf.compat.v1.keras.initializers.VarianceScaling(scale=1.0, mode="fan_avg", distribution="uniform", seed = 1))
    x_b2 = tf.compat.v1.get_variable("x_b2", [16], initializer = tf.compat.v1.zeros_initializer())    

    
    parameters = {"x_w1": x_w1,
                  "x_b1": x_b1,
                  "x_w2": x_w2,
                  "x_b2": x_b2}
                  
    return parameters

def GCN_layer(x_in, L_, weights):

    x_mid = tf.matmul(x_in, weights)
    x_out = tf.matmul(L_, x_mid)
    
    return x_out

def mynetwork(x, parameters, Lap, isTraining, momentums = 0.9):

    with tf.compat.v1.name_scope("x_layer_1"):

         x_z1_bn = tf.compat.v1.layers.batch_normalization(x, momentum = momentums, training = isTraining)             
         x_z1 = GCN_layer(x_z1_bn, Lap, parameters['x_w1']) + parameters['x_b1']
         x_z1_bn = tf.compat.v1.layers.batch_normalization(x_z1, momentum = momentums, training = isTraining)
         x_a1 = tf.nn.relu(x_z1_bn)      
         
    with tf.compat.v1.name_scope("x_layer_3"):
        
         x_z2_bn = tf.compat.v1.layers.batch_normalization(x_a1, momentum = momentums, training = isTraining)        
         x_z2 = GCN_layer(x_z2_bn, Lap, parameters['x_w2']) + parameters['x_b2']         

    l2_loss =  tf.nn.l2_loss(parameters['x_w1']) + tf.nn.l2_loss(parameters['x_w2'])
                
    return x_z2, l2_loss

def mynetwork_optimaization(y_est, y_re, l2_loss, reg, learning_rate, global_step):
    
    y_re = tf.squeeze(y_re, name = 'y_re')
    
    with tf.compat.v1.name_scope("cost"):
         cost = tf.reduce_mean(input_tensor=tf.nn.softmax_cross_entropy_with_logits(logits = y_est, labels = tf.stop_gradient( y_re))) +  reg * l2_loss
         
    with tf.compat.v1.name_scope("optimization"):
         update_ops = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.UPDATE_OPS)
    with tf.control_dependencies(update_ops):
         optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate = learning_rate).minimize(cost,  global_step=global_step)
         optimizer = tf.group([optimizer, update_ops])
         
    return cost, optimizer

def network_accuracy(x_out, y_in):
    
    correct_prediction = tf.equal(tf.argmax(input=x_out, axis=1), tf.argmax(input=y_in, axis=1))
    accuracy = tf.reduce_mean(input_tensor=tf.cast(correct_prediction, "float"))
         
    return accuracy
    
def train_mynetwork(x_train, x_test, y_train, y_test, L_train, L_test, learning_rate_base = 0.001, beta_reg = 0.001, num_epochs = 200, minibatch_size = 32, print_cost = True):
    
    ops.reset_default_graph()    
    tf.compat.v1.set_random_seed(1) # tf.random.set_seed(1)                 
    seed = 1                                                         
    (m, n_x) = x_train.shape
    (m, n_y) = y_train.shape
    
    costs = []                                        
    costs_dev = []
    train_acc = []
    val_acc = []
    
    x_in, y_in, lap_train, isTraining = create_placeholders(n_x, n_y) 

    parameters = initialize_parameters()
    
    with tf.compat.v1.name_scope("network"):
         x_out, l2_loss = mynetwork(x_in, parameters, lap_train, isTraining)

    global_step = tf.Variable(0, trainable=False)
    learning_rate = tf.compat.v1.train.exponential_decay(learning_rate_base, global_step, 50 * m/minibatch_size, 0.5, staircase = True)
    
    with tf.compat.v1.name_scope("optimization"):
         cost, optimizer = mynetwork_optimaization(x_out, y_in, l2_loss, beta_reg, learning_rate, global_step)

    with tf.compat.v1.name_scope("metrics"):
         accuracy = network_accuracy(x_out, y_in)

    init = tf.compat.v1.global_variables_initializer()

    with tf.compat.v1.Session() as sess:
        
        sess.run(init)
      
        # Do the training loop
        for epoch in range(num_epochs + 1):
            epoch_cost = 0.
            epoch_acc = 0.
            
            num_minibatches = int(m / minibatch_size) # number of minibatches of size minibatch_size in the train set
            seed = seed + 1
            
            minibatches = random_mini_batches_GCN(x_train, y_train, L_train, minibatch_size, seed)
            
            for minibatch in minibatches:

                # Select a minibatch
                (batch_x, batch_y, batch_l) = minibatch
                # IMPORTANT: The line that runs the graph on a minibatch.
                # Run the session to execute the "optimizer" and the "cost", the feedict should contain a minibatch for (X,Y).
                _, minibatch_cost, minibatch_acc = sess.run([optimizer, cost, accuracy], feed_dict={x_in: batch_x, y_in: batch_y, lap_train: batch_l, isTraining: True})           
                epoch_cost += minibatch_cost 
                epoch_acc += minibatch_acc

            epoch_cost_train = epoch_cost / (num_minibatches+ 1) 
            epoch_acc_train = epoch_acc / (num_minibatches+ 1) 

           
            if print_cost == True and epoch % 50 == 0:
                features, epoch_cost_test, epoch_acc_test = sess.run([x_out, cost, accuracy], feed_dict={x_in: x_test, y_in: y_test, lap_train: L_test, isTraining: False})
                print ("epoch %i: Train_loss: %f, Val_loss: %f, Train_acc: %f, Val_acc: %f" % (epoch, epoch_cost_train, epoch_cost_test, epoch_acc_train, epoch_acc_test))
            
            if print_cost == True and epoch % 5 == 0:
                costs.append(epoch_cost_train)
                train_acc.append(epoch_acc_train)
                costs_dev.append(epoch_cost_test)
                val_acc.append(epoch_acc_test)

        # plot the cost      
        plt.plot(np.squeeze(costs))
        plt.plot(np.squeeze(costs_dev))
        plt.ylabel('cost')
        plt.xlabel('iterations (per tens)')
        plt.title("Learning rate =" + str(learning_rate))
        plt.show()
        
        # plot the accuracy 
        plt.plot(np.squeeze(train_acc))
        plt.plot(np.squeeze(val_acc))
        plt.ylabel('accuracy')
        plt.xlabel('iterations (per tens)')
        plt.title("Learning rate =" + str(learning_rate))
        plt.show()
      
        # lets save the parameters in a variable
        parameters = sess.run(parameters)
        print ("Parameters have been trained!")
       
       
        return parameters, val_acc, features

if __name__ == '__main__':
    start()
    try:
        Train_X = scio.loadmat('HSI_GCN/Train_X.mat')
        TrLabel = scio.loadmat('HSI_GCN/TrLabel.mat')
        Test_X = scio.loadmat('HSI_GCN/Test_X.mat')
        TeLabel = scio.loadmat('HSI_GCN/TeLabel.mat')
        Train_L = scio.loadmat('HSI_GCN/Train_L.mat')
        Test_L = scio.loadmat('HSI_GCN/Test_L.mat')


        Train_X = Train_X['Train_X']
        Test_X = Test_X['Test_X']
        TrLabel = TrLabel['TrLabel']
        TeLabel = TeLabel['TeLabel']

        Train_L = Train_L['Train_L']
        Test_L = Test_L['Test_L']

        TrLabel = convert_to_one_hot(TrLabel-1, 16)
        TrLabel = TrLabel.T
        TeLabel = convert_to_one_hot(TeLabel-1, 16)   
        TeLabel = TeLabel.T


        parameters, val_acc, features = train_mynetwork(Train_X, Test_X, TrLabel, TeLabel, Train_L, Test_L)
        sio.savemat('features.mat', {'features': features})
    finally:
        stop()
        
